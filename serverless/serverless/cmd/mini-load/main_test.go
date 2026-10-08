package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestRejectionsCountAndQualificationFails(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/metrics" {
			json.NewEncoder(w).Encode(metrics{Workers: []worker{{Name: "one", Node: "one", Ready: true, BootID: "generation"}}})
			return
		}
		w.WriteHeader(429)
		w.Write([]byte(`{"error":"CPU exhausted","admitted":false}`))
	}))
	defer server.Close()
	client := server.Client()
	c := config{url: server.URL, token: "token", servers: 1, duration: .05, rate: 600, maxInflight: 20, timeout: 1, threshold: 1}
	r, err := stage(client, c, []byte(`{}`), 10)
	if err != nil {
		t.Fatal(err)
	}
	if r["passes_slo"].(bool) || r["error_fraction"].(float64) != 1 || r["resource_rejected"].(int) != r["planned_requests"].(int) {
		t.Fatalf("ignored rejections %+v", r)
	}
}

func TestSlowCompletionsAreMeasuredWithoutStoppingRamp(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			json.NewEncoder(w).Encode(metrics{Workers: []worker{{Name: "one", Node: "one", Ready: true, BootID: "same"}}})
			return
		}
		calls.Add(1)
		time.Sleep(30 * time.Millisecond)
		w.Write([]byte(`{"metrics":{"startup_ms":2,"vm_startup_ms":50,"vm_boot_ms":45,"execution_ms":10,"total_ms":22,"pool_hit":true,"worker_node":"one"}}`))
	}))
	defer server.Close()
	c := config{url: server.URL, servers: 1, duration: .01, rate: 60, maxInflight: 20, timeout: 1, threshold: .001}
	for _, users := range []int{1, 2} {
		r, err := stage(server.Client(), c, []byte(`{}`), users)
		if err != nil {
			t.Fatal(err)
		}
		if r["passes_slo"].(bool) || r["completed"].(int) != 1 || rampStopReason(r) != "" {
			t.Fatalf("slow success stopped exploratory ramp: %+v", r)
		}
		completion := r["request_completion_ms"].(map[string]any)
		if completion["p50"].(float64) < 30 || completion["p95"].(float64) < 30 {
			t.Fatal("completion time omitted server delay")
		}
		startup := r["vm_startup_ms"].(map[string]any)
		if startup["p95"].(float64) != 50 {
			t.Fatal("preboot cost replaced by assignment latency")
		}
	}
	if calls.Load() != 2 {
		t.Fatal("next user level was not executed")
	}
}
func TestBootGenerationChangesIdentity(t *testing.T) {
	m := metrics{Workers: []worker{{Name: "one", Node: "one", Ready: true, BootID: "first"}}}
	a, _ := identity(m, 1)
	m.Workers[0].BootID = "second"
	b, _ := identity(m, 1)
	if a == b {
		t.Fatal("restart not detected")
	}
}
func TestSyntheticResultsCannotClaimGuestCapacity(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/metrics" {
			json.NewEncoder(w).Encode(metrics{Workers: []worker{{Name: "one", Node: "one", Ready: true, BootID: "generation"}}})
			return
		}
		w.Write([]byte(`{"metrics":{"synthetic":true,"total_ms":0}}`))
	}))
	defer server.Close()
	r, err := stage(server.Client(), config{url: server.URL, servers: 1, duration: .02, rate: 60, maxInflight: 20, timeout: 1, threshold: 1}, []byte(`{}`), 60)
	if err != nil {
		t.Fatal(err)
	}
	if r["completed"].(int) != 0 || r["passes_slo"].(bool) {
		t.Fatal("synthetic throughput was called guest capacity")
	}
}
