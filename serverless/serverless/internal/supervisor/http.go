package supervisor

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

type job struct {
	done      chan struct{}
	cancel    context.CancelFunc
	result    *Result
	err       error
	created   time.Time
	completed time.Time
	size      int
}
type Service struct {
	scheduleState string
	TestExecutor  func(context.Context, Spec, int, int) (*Result, error)
	Engine        *Engine
	Token         string
	mu            sync.Mutex
	jobs          map[string]*job
	closed        bool
	resultBytes   int
	schedules     map[string]*schedule
	ctx           context.Context
	cancel        context.CancelFunc
}

func NewService(e *Engine, token string) *Service {
	ctx, cancel := context.WithCancel(e.ctx)
	s := &Service{Engine: e, Token: token, jobs: make(map[string]*job), schedules: make(map[string]*schedule), ctx: ctx, cancel: cancel}
	go s.maintenance()
	return s
}
func reply(w http.ResponseWriter, status int, v any) {
	data, err := json.Marshal(v)
	if err != nil {
		status = 500
		data = []byte(`{"error":"response encoding failed"}`)
	}
	data = append(data, '\n')
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	w.WriteHeader(status)
	w.Write(data)
}
func failure(w http.ResponseWriter, err error) {
	status := 500
	var rejected *Rejection
	if errors.As(err, &rejected) {
		status = 429
	}
	if errors.Is(err, context.DeadlineExceeded) {
		status = 504
	}
	reply(w, status, map[string]any{"error": err.Error(), "admitted": status != 429 || rejected.Admitted})
}

type submission struct {
	Depth int  `json:"depth"`
	Spec  Spec `json:"spec"`
	Count int  `json:"count"`
}

func decode(w http.ResponseWriter, r *http.Request, v any) error {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 100000))
	if err := d.Decode(v); err != nil {
		return err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return fmt.Errorf("expected one JSON value")
	}
	return nil
}
func (s *Service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/healthz" && subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.Token)) != 1 {
		reply(w, 401, map[string]string{"error": "unauthorized"})
		return
	}
	switch r.URL.Path {
	case "/healthz":
		reply(w, 200, map[string]bool{"ready": true})
		return
	case "/metrics":
		reply(w, 200, s.Engine.Metrics())
		return
	case "/v1/schedules":
		s.scheduleHTTP(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/v1/schedules/") {
		s.scheduleHTTP(w, r)
		return
	}
	if r.Method == "POST" && (r.URL.Path == "/v1/invoke" || r.URL.Path == "/v1/jobs") {
		var sub submission
		if err := decode(w, r, &sub); err != nil {
			reply(w, 400, map[string]string{"error": err.Error()})
			return
		}
		if sub.Count == 0 {
			sub.Count = 1
		}
		if err := sub.Spec.Validate(); err != nil {
			reply(w, 400, map[string]string{"error": err.Error()})
			return
		}
		if sub.Count < 1 || sub.Count > 256 || sub.Depth < 0 || sub.Depth > 8 {
			reply(w, 400, map[string]string{"error": "count must be 1..256"})
			return
		}
		if r.URL.Path == "/v1/invoke" {
			result, err := s.invoke(r.Context(), sub.Spec, sub.Count, sub.Depth)
			if err != nil {
				failure(w, err)
			} else {
				encoded, _ := json.Marshal(result)
				if len(encoded) > 1024*1024 {
					failure(w, fmt.Errorf("result exceeds 1 MiB"))
				} else {
					reply(w, 200, result)
				}
			}
			return
		}
		s.mu.Lock()
		if len(s.jobs) >= 4096 || s.closed {
			s.mu.Unlock()
			failure(w, &Rejection{Reason: "job metadata capacity exhausted"})
			return
		}
		ctx, cancel := context.WithTimeout(s.ctx, time.Duration(sub.Spec.Timeout)*time.Second)
		j := &job{done: make(chan struct{}), cancel: cancel, created: time.Now()}
		key := id()
		s.jobs[key] = j
		s.mu.Unlock()
		go func() {
			defer cancel()
			result, err := s.invoke(ctx, sub.Spec, sub.Count, sub.Depth)
			encoded, _ := json.Marshal(result)
			s.mu.Lock()
			if len(encoded) > 1024*1024 || s.resultBytes+len(encoded) > 16*1024*1024 {
				result = nil
				err = fmt.Errorf("result retention budget exceeded")
			}
			j.result = result
			j.err = err
			j.completed = time.Now()
			if result != nil {
				j.size = len(encoded)
				s.resultBytes += j.size
			}
			close(j.done)
			s.mu.Unlock()
		}()
		reply(w, 202, map[string]any{"job_id": key, "wait_supported": true})
		return
	}
	if strings.HasPrefix(r.URL.Path, "/v1/jobs/") {
		key := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/v1/jobs/"), "/wait")
		s.mu.Lock()
		j := s.jobs[key]
		s.mu.Unlock()
		if j == nil {
			reply(w, 410, map[string]string{"error": "job absent or expired"})
			return
		}
		if r.Method == "DELETE" {
			j.cancel()
			reply(w, 200, map[string]bool{"cancelled": true})
			return
		}
		if r.Method != "GET" {
			reply(w, 405, map[string]string{"error": "method not allowed"})
			return
		}
		if strings.HasSuffix(r.URL.Path, "/wait") {
			select {
			case <-j.done:
			case <-r.Context().Done():
				j.cancel()
				return
			}
		}
		select {
		case <-j.done:
			if j.err != nil {
				reply(w, 200, map[string]any{"phase": "failed", "error": j.err.Error()})
			} else {
				reply(w, 200, map[string]any{"phase": "complete", "result": j.result})
			}
		default:
			reply(w, 200, map[string]string{"phase": "running"})
		}
		return
	}
	reply(w, 404, map[string]string{"error": "unknown route"})
}
func (s *Service) maintenance() {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case now := <-t.C:
			s.mu.Lock()
			for key, j := range s.jobs {
				select {
				case <-j.done:
					if now.Sub(j.completed) > 60*time.Second {
						s.resultBytes -= j.size
						delete(s.jobs, key)
					}
				default:
				}
			}
			for _, sc := range s.schedules {
				sc.tick(s, now)
			}
			s.mu.Unlock()
		}
	}
}
func (s *Service) Close() {
	s.mu.Lock()
	s.closed = true
	for _, j := range s.jobs {
		j.cancel()
	}
	s.mu.Unlock()
	s.cancel()
}

func (s *Service) invoke(ctx context.Context, spec Spec, count, depth int) (*Result, error) {
	if s.TestExecutor != nil {
		return s.TestExecutor(ctx, spec, count, depth)
	}
	return s.Engine.Invoke(ctx, spec, count, depth)
}

// Explicit synthetic baseline: same auth, decoding, invocation endpoint, and result encoding.
// It executes no guest and must never be reported as Mini OS capacity.
func NewBenchmarkService(ctx context.Context, token string) *Service {
	e := &Engine{ctx: ctx, cfg: Config{Name: "synthetic", Node: "synthetic"}, bootID: id()}
	s := NewService(e, token)
	s.TestExecutor = func(ctx context.Context, spec Spec, count, depth int) (*Result, error) {
		return &Result{ID: id(), Output: "synthetic", Metrics: map[string]any{"synthetic": true, "startup_ms": 0., "execution_ms": 0., "total_ms": 0., "worker_node": "synthetic"}}, nil
	}
	return s
}
