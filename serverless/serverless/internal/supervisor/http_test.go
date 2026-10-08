package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testToken = "0123456789abcdef0123456789abcdef"

func TestNoRetryAfterAdmission(t *testing.T) {
	var invoked atomic.Int32
	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			reply(w, 200, map[string]any{"workers": []any{map[string]any{"name": "one", "node": "one", "ready": true}}})
			return
		}
		invoked.Add(1)
		failure(w, postAdmission(&Rejection{Reason: "child rejected"}))
	}))
	defer first.Close()
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			reply(w, 200, map[string]any{"workers": []any{map[string]any{"name": "two", "node": "two", "ready": true}}})
			return
		}
		invoked.Add(100)
		reply(w, 200, map[string]bool{"duplicate": true})
	}))
	defer second.Close()
	router, err := NewRouter(context.Background(), first.URL+","+second.URL, testToken)
	if err != nil {
		t.Fatal(err)
	}
	defer router.Close()
	req := httptest.NewRequest("POST", "/v1/invoke", strings.NewReader(`{"spec":{"script":"print(1);"}}`))
	req.Header.Set("Authorization", "Bearer "+testToken)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != 429 || invoked.Load() != 1 {
		t.Fatalf("replayed admitted invocation: %d %d", w.Code, invoked.Load())
	}
}
func TestRefreshKeepsLiveCounters(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			reply(w, 200, map[string]any{"workers": []any{map[string]any{"name": "one", "node": "one", "ready": true}}})
			return
		}
		close(started)
		<-release
		reply(w, 200, map[string]bool{"done": true})
	}))
	defer server.Close()
	router, err := NewRouter(context.Background(), server.URL, testToken)
	if err != nil {
		t.Fatal(err)
	}
	defer router.Close()
	done := make(chan struct{})
	go func() {
		defer close(done)
		req := httptest.NewRequest("POST", "/v1/invoke", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer "+testToken)
		router.ServeHTTP(httptest.NewRecorder(), req)
	}()
	<-started
	if err := router.refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	close(release)
	<-done
	router.mu.Lock()
	defer router.mu.Unlock()
	if router.workers[0].active != 0 {
		t.Fatal("refresh retained stale activity")
	}
}
func TestRouterMetadataRejectsBeforeSubmit(t *testing.T) {
	var submits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			reply(w, 200, map[string]any{"workers": []any{map[string]any{"name": "one", "node": "one", "ready": true}}})
			return
		}
		submits.Add(1)
		reply(w, 202, map[string]string{"job_id": "orphan"})
	}))
	defer server.Close()
	router, err := NewRouter(context.Background(), server.URL, testToken)
	if err != nil {
		t.Fatal(err)
	}
	defer router.Close()
	router.mu.Lock()
	for i := 0; i < 4096; i++ {
		router.jobs[fmt.Sprint(i)] = routeJob{server.URL, time.Now().Add(time.Hour)}
	}
	router.mu.Unlock()
	req := httptest.NewRequest("POST", "/v1/jobs", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+testToken)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != 429 || submits.Load() != 0 {
		t.Fatal("submitted an unrouteable job")
	}
}
func TestExactCPUSettlement(t *testing.T) {
	e := &Engine{credits: 1, cost: .1, guests: map[*Guest]*reservation{}}
	g := &Guest{cpu: .04, rss: 40 * 1024 * 1024, memory: 8}
	res := &reservation{bytes: 40 * 1024 * 1024, charged: .2}
	e.reserved = res.bytes
	e.cost = .3
	e.release(g, res)
	if e.credits < 1.159 || e.credits > 1.161 || e.reserved != 0 {
		t.Fatalf("refunded changed estimate: %+v", e)
	}
}
func TestDurableSchedulesAndCronDaySemantics(t *testing.T) {
	s := NewBenchmarkService(context.Background(), testToken)
	defer s.Close()
	path := t.TempDir() + "/schedules.json"
	if err := s.SetScheduleState(path); err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("POST", "/v1/schedules", strings.NewReader(`{"name":"test","cron":"0 0 1 * 1","timezone":"UTC","spec":{"script":"print(1);"}}`))
	req.Header.Set("Authorization", "Bearer "+testToken)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	s2 := NewBenchmarkService(context.Background(), testToken)
	defer s2.Close()
	if err := s2.SetScheduleState(path); err != nil {
		t.Fatal(err)
	}
	sc := s2.schedules["test"]
	if sc == nil || !sc.matches(time.Date(2026, 10, 12, 0, 0, 0, 0, time.UTC)) {
		t.Fatal("Monday should match cron even when day is not 1")
	}
}
func TestSyntheticSameInvocationEndpoint(t *testing.T) {
	s := NewBenchmarkService(context.Background(), testToken)
	defer s.Close()
	req := httptest.NewRequest("POST", "/v1/invoke", strings.NewReader(`{"spec":{"script":"print(1);"}}`))
	req.Header.Set("Authorization", "Bearer "+testToken)
	w := httptest.NewRecorder()
	s.ServeHTTP(w, req)
	var r Result
	json.Unmarshal(w.Body.Bytes(), &r)
	if w.Code != 200 || r.Metrics["synthetic"] != true {
		t.Fatal("baseline not labelled synthetic")
	}
}

func TestRoutedJobsSurviveRouterReplicaChange(t *testing.T) {
	a := &Router{token: testToken}
	b := &Router{token: testToken}
	key := a.encodeJobID("http://10.0.0.1:8181", "job-identity")
	url, id, err := b.decodeJobID(key)
	if err != nil || url != "http://10.0.0.1:8181" || id != "job-identity" {
		t.Fatal("replica cannot route job", err)
	}
	if _, _, err = b.decodeJobID(key + "tampered"); err == nil {
		t.Fatal("tampered routing accepted")
	}
}
