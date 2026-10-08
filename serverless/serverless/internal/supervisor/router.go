package supervisor

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type routeWorker struct {
	url     string
	active  int
	metrics map[string]any
}
type routeJob struct {
	url     string
	expires time.Time
}
type Router struct {
	submissions   int
	token, config string
	client        *http.Client
	mu            sync.Mutex
	workers       []*routeWorker
	jobs          map[string]routeJob
	next          uint64
	cancel        context.CancelFunc
	done          chan struct{}
}

func NewRouter(ctx context.Context, config, token string) (*Router, error) {
	if config == "" {
		return nil, fmt.Errorf("provide --workers URLs or dns:headless-service:port")
	}
	ctx, cancel := context.WithCancel(ctx)
	r := &Router{config: config, token: token, jobs: map[string]routeJob{}, cancel: cancel, done: make(chan struct{}), client: &http.Client{Transport: &http.Transport{MaxIdleConns: 4096, MaxIdleConnsPerHost: 1024, MaxConnsPerHost: 2048, IdleConnTimeout: 30 * time.Second, DialContext: (&net.Dialer{Timeout: time.Second, KeepAlive: 30 * time.Second}).DialContext}, Timeout: 610 * time.Second}}
	if err := r.refresh(ctx); err != nil {
		cancel()
		return nil, err
	}
	go func() {
		defer close(r.done)
		t := time.NewTicker(time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				r.refresh(ctx)
			}
		}
	}()
	return r, nil
}
func (r *Router) refresh(ctx context.Context) error {
	urls := strings.Split(r.config, ",")
	if strings.HasPrefix(r.config, "dns:") {
		host, port, err := net.SplitHostPort(strings.TrimPrefix(r.config, "dns:"))
		if err != nil {
			return err
		}
		ips, err := net.DefaultResolver.LookupHost(ctx, host)
		if err != nil {
			return err
		}
		urls = nil
		for _, ip := range ips {
			urls = append(urls, "http://"+net.JoinHostPort(ip, port))
		}
	}
	var workers []*routeWorker
	for _, url := range urls {
		if !strings.HasPrefix(url, "http://") {
			return fmt.Errorf("worker URLs must be http://")
		}
		pc, cancel := context.WithTimeout(ctx, time.Second)
		req, _ := http.NewRequestWithContext(pc, "GET", url+"/metrics", nil)
		req.Header.Set("Authorization", "Bearer "+r.token)
		resp, err := r.client.Do(req)
		if err != nil {
			cancel()
			continue
		}
		var data struct{ Workers []map[string]any }
		err = json.NewDecoder(io.LimitReader(resp.Body, 100000)).Decode(&data)
		resp.Body.Close()
		cancel()
		if err == nil && resp.StatusCode == 200 && len(data.Workers) == 1 {
			workers = append(workers, &routeWorker{url: url, metrics: data.Workers[0]})
		}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, nw := range workers {
		for _, old := range r.workers {
			if nw.url == old.url {
				old.metrics = nw.metrics
				workers[i] = old
			}
		}
	}
	r.workers = workers
	for key, v := range r.jobs {
		if time.Now().After(v.expires) {
			delete(r.jobs, key)
		}
	}
	return nil
}
func (r *Router) Close() { r.cancel(); <-r.done; r.client.CloseIdleConnections() }
func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.URL.Path != "/healthz" && req.Header.Get("Authorization") != "Bearer "+r.token {
		reply(w, 401, map[string]string{"error": "unauthorized"})
		return
	}
	if req.URL.Path == "/healthz" {
		r.mu.Lock()
		ready := len(r.workers) > 0
		r.mu.Unlock()
		status := 200
		if !ready {
			status = 503
		}
		reply(w, status, map[string]bool{"ready": ready})
		return
	}
	if req.URL.Path == "/metrics" {
		r.mu.Lock()
		var workers []any
		pending := 0
		for _, v := range r.workers {
			m := map[string]any{}
			for k, x := range v.metrics {
				m[k] = x
			}
			m["routing_active"] = v.active
			pending += v.active
			workers = append(workers, m)
		}
		r.mu.Unlock()
		reply(w, 200, map[string]any{"backend": "go-worker-pool", "workers": workers, "pending": pending})
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, req.Body, 100000))
	if err != nil {
		reply(w, 400, map[string]string{"error": err.Error()})
		return
	}
	isSubmission := req.Method == "POST" && req.URL.Path == "/v1/jobs"
	if isSubmission {
		r.mu.Lock()
		if len(r.jobs)+r.submissions >= 4096 {
			r.mu.Unlock()
			failure(w, &Rejection{Reason: "routing metadata full"})
			return
		}
		r.submissions++
		r.mu.Unlock()
		defer func() { r.mu.Lock(); r.submissions--; r.mu.Unlock() }()
	}
	if strings.HasPrefix(req.URL.Path, "/v1/schedules") && os.Getenv("KUBERNETES_SERVICE_HOST") != "" {
		data, _ := json.Marshal(map[string]any{"method": req.Method, "path": req.URL.Path, "body": json.RawMessage(body)})
		cmd := exec.CommandContext(req.Context(), "python3", "-m", "remote_desktop.function_schedule")
		cmd.Stdin = bytes.NewReader(data)
		out, err := cmd.Output()
		if err != nil {
			failure(w, fmt.Errorf("schedule management: %w", err))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(202)
		w.Write(out)
		return
	}
	r.mu.Lock()
	var candidates []*routeWorker
	forwardPath := req.URL.RequestURI()
	if strings.HasPrefix(req.URL.Path, "/v1/jobs/") {
		key := strings.TrimSuffix(strings.TrimPrefix(req.URL.Path, "/v1/jobs/"), "/wait")
		if url, internal, err := r.decodeJobID(key); err == nil {
			candidates = []*routeWorker{{url: url}}
			forwardPath = "/v1/jobs/" + internal
			if strings.HasSuffix(req.URL.Path, "/wait") {
				forwardPath += "/wait"
			}
		} else if v, ok := r.jobs[key]; ok {
			candidates = []*routeWorker{{url: v.url}}
		}
	} else {
		for _, v := range r.workers {
			candidates = append(candidates, v)
		}
		if len(candidates) > 0 {
			offset := int(r.next % uint64(len(candidates)))
			r.next++
			candidates = append(candidates[offset:], candidates[:offset]...)
			sort.SliceStable(candidates, func(i, j int) bool { a, b := candidates[i], candidates[j]; return a.active < b.active })
		}
	}
	r.mu.Unlock()
	if len(candidates) == 0 {
		if strings.HasPrefix(req.URL.Path, "/v1/jobs/") {
			reply(w, 410, map[string]string{"error": "job absent or expired"})
			return
		}
		reply(w, 503, map[string]string{"error": "no ready workers"})
		return
	}
	admissionBegan := time.Now()
	// Only explicit nonadmission may be retried. Network/timeouts are ambiguous.
	for _, worker := range candidates {
		r.mu.Lock()
		worker.active++
		r.mu.Unlock()
		request, _ := http.NewRequestWithContext(req.Context(), req.Method, worker.url+forwardPath, bytes.NewReader(body))
		request.Header.Set("Authorization", "Bearer "+r.token)
		request.Header.Set("Content-Type", "application/json")
		response, err := r.client.Do(request)
		r.mu.Lock()
		worker.active--
		r.mu.Unlock()
		if err != nil {
			reply(w, 502, map[string]string{"error": err.Error()})
			return
		}
		result, err := io.ReadAll(io.LimitReader(response.Body, 4*1024*1024+1))
		response.Body.Close()
		if err != nil || len(result) > 4*1024*1024 {
			reply(w, 502, map[string]string{"error": "invalid worker response"})
			return
		}
		if response.StatusCode == 429 {
			var rejection struct{ Admitted *bool }
			json.Unmarshal(result, &rejection)
			if rejection.Admitted != nil && !*rejection.Admitted {
				if time.Since(admissionBegan) >= 25*time.Millisecond {
					failure(w, &Rejection{Reason: "admission budget exhausted"})
					return
				}
				continue
			}
		}
		if response.StatusCode == 202 && req.URL.Path == "/v1/jobs" {
			var created struct {
				ID string `json:"job_id"`
			}
			if json.Unmarshal(result, &created) == nil {
				r.mu.Lock()
				if created.ID != "" {
					publicID := r.encodeJobID(worker.url, created.ID)
					r.jobs[publicID] = routeJob{worker.url, time.Now().Add(11 * time.Minute)}
					var ack map[string]any
					json.Unmarshal(result, &ack)
					ack["job_id"] = publicID
					result, _ = json.Marshal(ack)
				}
				r.mu.Unlock()
			}
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", strconv.Itoa(len(result)))
		w.WriteHeader(response.StatusCode)
		w.Write(result)
		return
	}
	failure(w, &Rejection{Reason: "all workers lack CPU or memory headroom"})
}

func (r *Router) encodeJobID(url, id string) string {
	body := base64.RawURLEncoding.EncodeToString([]byte(url)) + "~" + id
	mac := hmac.New(sha256.New, []byte(r.token))
	mac.Write([]byte(body))
	return body + "~" + hex.EncodeToString(mac.Sum(nil)[:16])
}
func (r *Router) decodeJobID(key string) (string, string, error) {
	p := strings.Split(key, "~")
	if len(p) != 3 {
		return "", "", fmt.Errorf("invalid routed job")
	}
	mac := hmac.New(sha256.New, []byte(r.token))
	mac.Write([]byte(p[0] + "~" + p[1]))
	signature, err := hex.DecodeString(p[2])
	if err != nil || !hmac.Equal(signature, mac.Sum(nil)[:16]) {
		return "", "", fmt.Errorf("invalid routed job signature")
	}
	url, err := base64.RawURLEncoding.DecodeString(p[0])
	if err != nil || !strings.HasPrefix(string(url), "http://") {
		return "", "", fmt.Errorf("invalid routed job URL")
	}
	return string(url), p[1], nil
}
