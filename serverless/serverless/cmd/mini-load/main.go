// Direct, staggered arrivals. Reuses TCP connections and counts every planned arrival.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"os"
	"sort"
	"sync"
	"time"
)

type config struct {
	url, token, script, output                       string
	start, target, maxInflight, servers, repetitions int
	duration, rate, threshold, timeout               float64
	qualify, transport                               bool
	verbose                                          bool
	jobTimeout                                       int
}
type worker struct {
	Name, Node string
	BootID     string `json:"boot_id"`
	Ready      bool
}
type metrics struct {
	Workers []worker
	Pending int
}

func request(ctx context.Context, client *http.Client, c config, method, path string, body []byte) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, method, c.url+path, bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4*1024*1024))
	return b, resp.StatusCode, err
}
func dist(values []float64) map[string]any {
	sort.Float64s(values)
	r := map[string]any{"samples": len(values)}
	for key, p := range map[string]float64{"p50": .5, "p95": .95, "p99": .99, "max": 1} {
		if len(values) == 0 {
			r[key] = nil
		} else {
			r[key] = values[int(math.Ceil(float64(len(values))*p))-1]
		}
	}
	return r
}
func identity(m metrics, n int) (string, error) {
	if len(m.Workers) != n {
		return "", fmt.Errorf("need exactly %d workers, got %d", n, len(m.Workers))
	}
	nodes := map[string]bool{}
	var names []string
	for _, w := range m.Workers {
		if !w.Ready || nodes[w.Node] {
			return "", fmt.Errorf("workers unready or share hosting node")
		}
		nodes[w.Node] = true
		names = append(names, w.Name+"@"+w.Node+"#"+w.BootID)
	}
	sort.Strings(names)
	return fmt.Sprint(names), nil
}
func stage(client *http.Client, c config, body []byte, users int) (map[string]any, error) {
	fetch := func() (metrics, []byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		b, status, err := request(ctx, client, c, "GET", "/metrics", nil)
		var m metrics
		if err == nil && status != 200 {
			err = fmt.Errorf("metrics HTTP %d", status)
		}
		if err == nil {
			err = json.Unmarshal(b, &m)
		}
		return m, b, err
	}
	before, _, err := fetch()
	if err != nil {
		return nil, err
	}
	population, err := identity(before, c.servers)
	if err != nil {
		return nil, err
	}
	if before.Pending != 0 {
		return nil, fmt.Errorf("service has unfinished invocations")
	}
	rate := float64(users) * c.rate / 60
	planned := int(math.Ceil(rate * c.duration))
	began := time.Now()
	end := began.Add(time.Duration(c.duration * 1e9))
	var mu sync.Mutex
	var wg sync.WaitGroup
	active, peak, sent, completed, rejected, failed, dropped, inWindow := 0, 0, 0, 0, 0, 0, 0, 0
	values := map[string][]float64{}
	failures := map[string]int{}
	nodes := map[string]int{}
	poolHits := 0
	var timeline []json.RawMessage
	stable := true
	monitorCtx, stopMonitor := context.WithCancel(context.Background())
	monitorDone := make(chan struct{})
	go func() {
		defer close(monitorDone)
		t := time.NewTicker(500 * time.Millisecond)
		defer t.Stop()
		for {
			select {
			case <-monitorCtx.Done():
				return
			case <-t.C:
				m, b, err := fetch()
				mu.Lock()
				if err != nil {
					stable = false
				} else {
					key, err := identity(m, c.servers)
					if err != nil || key != population {
						stable = false
					}
					timeline = append(timeline, b)
				}
				mu.Unlock()
			}
		}
	}()
	for index := 0; index < planned; index++ {
		due := began.Add(time.Duration(float64(index) / rate * 1e9))
		if wait := time.Until(due); wait > 0 {
			time.Sleep(wait)
		}
		if !time.Now().Before(end) {
			mu.Lock()
			dropped += planned - index
			mu.Unlock()
			break
		}
		mu.Lock()
		if active >= c.maxInflight {
			dropped++
			mu.Unlock()
			continue
		}
		active++
		sent++
		peak = max(peak, active)
		mu.Unlock()
		wg.Add(1)
		go func(due time.Time) {
			defer wg.Done()
			dispatch := time.Now()
			ctx, cancel := context.WithTimeout(context.Background(), time.Duration(c.timeout*1e9))
			defer cancel()
			path := "/v1/invoke"
			method := "POST"
			if c.transport {
				path = "/v1/invoke"
			}
			b, status, err := request(ctx, client, c, method, path, body)
			finished := time.Now()
			mu.Lock()
			defer mu.Unlock()
			active--
			values["dispatch_lag_ms"] = append(values["dispatch_lag_ms"], dispatch.Sub(due).Seconds()*1000)
			if err != nil || status != 200 {
				if status == 429 {
					rejected++
					values["rejection_latency_ms"] = append(values["rejection_latency_ms"], finished.Sub(due).Seconds()*1000)
				} else {
					failed++
				}
				key := fmt.Sprintf("HTTP %d: %s", status, b)
				if err != nil {
					key = err.Error()
				}
				if len(key) > 256 {
					key = key[:256]
				}
				failures[key]++
				return
			}
			if c.transport {
				var synthetic struct{ Metrics map[string]any }
				if json.Unmarshal(b, &synthetic) != nil || synthetic.Metrics["synthetic"] != true {
					failed++
					failures["transport baseline requires --benchmark-echo service"]++
					return
				}
			}
			if !c.transport {
				var result struct{ Metrics map[string]any }
				if err = json.Unmarshal(b, &result); err != nil || result.Metrics == nil || result.Metrics["synthetic"] == true {
					failed++
					failures["malformed result"]++
					return
				}
				for _, key := range []string{"startup_ms", "vm_startup_ms", "vm_boot_ms", "disk_setup_ms", "cpu_wait_ms", "execution_ms", "total_ms", "lifecycle_cpu_ms", "peak_rss_bytes", "assignment_ms"} {
					if n, ok := result.Metrics[key].(float64); ok {
						values[key] = append(values[key], n)
					}
				}
				if n, ok := result.Metrics["total_ms"].(float64); ok {
					values["transport_and_router_ms"] = append(values["transport_and_router_ms"], math.Max(0, finished.Sub(dispatch).Seconds()*1000-n))
				}
				node, _ := result.Metrics["worker_node"].(string)
				nodes[node]++
				if hit, _ := result.Metrics["pool_hit"].(bool); hit {
					poolHits++
				}
			}
			completed++
			if finished.Before(end) {
				inWindow++
			}
			values["latency_ms"] = append(values["latency_ms"], finished.Sub(due).Seconds()*1000)
			values["request_completion_ms"] = append(values["request_completion_ms"], finished.Sub(dispatch).Seconds()*1000)
		}(due)
	}
	// Leave the offer window open until its stated duration, even at low arrival rates.
	if wait := time.Until(end); wait > 0 {
		time.Sleep(wait)
	}
	wg.Wait()
	deadline := time.Now().Add(time.Duration(c.timeout * 1e9))
	drained := false
	for time.Now().Before(deadline) {
		m, _, err := fetch()
		if err == nil && m.Pending == 0 {
			drained = true
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	stopMonitor()
	<-monitorDone
	result := map[string]any{"users": users, "offered_rps": rate, "duration_seconds": c.duration, "elapsed_with_drain_seconds": time.Since(began).Seconds(), "planned_requests": planned, "sent": sent, "completed": completed, "resource_rejected": rejected, "execution_or_transport_failed": failed, "generator_dropped": dropped, "error_fraction": float64(rejected+failed+dropped) / float64(max(1, planned)), "completed_rps_in_window": float64(inWindow) / c.duration, "peak_client_inflight": peak, "workers_stable": stable, "server_drained": drained, "pool_hits": poolHits, "worker_node_completions": nodes, "failures": failures, "resource_timeline": timeline}
	for key, v := range values {
		result[key] = dist(v)
	}
	p95 := math.Inf(1)
	if v := values["latency_ms"]; len(v) > 0 {
		p95 = dist(v)["p95"].(float64)
	}
	result["passes_slo"] = stable && drained && dropped == 0 && float64(rejected+failed)/float64(max(1, planned)) < .01 && p95 < c.threshold*1000
	return result, nil
}
func main() {
	var c config
	flag.StringVar(&c.url, "url", "http://127.0.0.1:8080", "service URL")
	flag.StringVar(&c.script, "script", "examples/HELLO.MOS", "source")
	flag.StringVar(&c.output, "output", "load-go.json", "report")
	flag.IntVar(&c.start, "start-users", 4096, "initial logical users")
	flag.IntVar(&c.target, "target-users", 1000000, "maximum logical users")
	flag.IntVar(&c.maxInflight, "max-inflight", 8192, "generator request bound")
	flag.IntVar(&c.servers, "servers", 1, "distinct hosting servers")
	flag.IntVar(&c.repetitions, "repetitions", 1, "repetitions per level")
	flag.Float64Var(&c.duration, "duration", 15, "offer window seconds")
	flag.Float64Var(&c.rate, "requests-per-user-minute", 1, "periodic requests/user/minute")
	flag.Float64Var(&c.threshold, "threshold-seconds", 1, "p95 SLO")
	flag.Float64Var(&c.timeout, "request-timeout", 40, "request deadline seconds")
	flag.IntVar(&c.jobTimeout, "job-timeout", 30, "guest job deadline seconds (1..600); separate from the reported latency SLO")
	flag.BoolVar(&c.verbose, "verbose", false, "also print full per-stage JSON; complete metrics are always saved")
	flag.BoolVar(&c.qualify, "qualify", false, "require 3 runs of >=600 seconds at one level")
	flag.BoolVar(&c.transport, "transport-only", false, "measure synthetic /v1/invoke control-plane baseline, not guest capacity")
	flag.Parse()
	c.token = os.Getenv("FUNCTION_TOKEN")
	if math.IsNaN(c.rate) || math.IsInf(c.rate, 0) || math.IsNaN(c.duration) || math.IsInf(c.duration, 0) || math.IsNaN(c.timeout) || math.IsInf(c.timeout, 0) || math.IsNaN(c.threshold) || math.IsInf(c.threshold, 0) || c.token == "" || c.start < 1 || c.target < c.start || c.rate <= 0 || c.duration <= 0 || c.maxInflight < 1 || c.repetitions < 1 || c.servers < 1 || c.timeout <= 0 || c.threshold <= 0 {
		fmt.Fprintln(os.Stderr, "invalid parameters or missing FUNCTION_TOKEN")
		os.Exit(2)
	}
	if c.jobTimeout < 1 || c.jobTimeout > 600 {
		fmt.Fprintln(os.Stderr, "job-timeout must be 1..600 seconds")
		os.Exit(2)
	}
	if c.qualify && (c.repetitions < 3 || c.duration < 600 || c.start != c.target || c.transport) {
		fmt.Fprintln(os.Stderr, "qualification needs >=3 repetitions, >=600 seconds, one level, real guests")
		os.Exit(2)
	}
	source, err := os.ReadFile(c.script)
	if err != nil {
		panic(err)
	}
	body, _ := json.Marshal(map[string]any{"spec": map[string]any{"script": string(source), "memory_mb": 8, "cpus": 1, "timeout_s": c.jobTimeout}})
	client := &http.Client{Transport: &http.Transport{MaxIdleConns: 8192, MaxIdleConnsPerHost: 8192, MaxConnsPerHost: c.maxInflight, IdleConnTimeout: 30 * time.Second, DialContext: (&net.Dialer{Timeout: 3 * time.Second, KeepAlive: 30 * time.Second}).DialContext}}
	defer client.CloseIdleConnections()
	report := map[string]any{"target_users": c.target, "requests_per_user_minute": c.rate, "qualification": c.qualify, "transport_only": c.transport, "partial_period_sample": c.duration*c.rate < 60, "load_model": "periodic_staggered_arrivals", "p95_threshold_seconds": c.threshold, "max_failure_fraction_exclusive": .01, "notes": []string{"Logical users describe traffic, not simultaneous open connections.", "Real-guest mode uses never-used guests; transport-only mode is synthetic and executes no guests. Background replenishment shares the service CPU/memory budget.", "A 15-second ramp is exploratory and does not establish sustained capacity.", "Generator and Lima VM share a physical host; qualification includes every planned arrival."}}
	var stages []map[string]any
	report["latency_stops_ramp"] = false
	report["timing_boundaries"] = map[string]string{
		"cpu_wait_ms":           "Worker admission wait before assigning or starting a guest; included in request completion time.",
		"request_completion_ms": "Client dispatch to complete successful response; includes transport and worker time.",
		"latency_ms":            "Scheduled arrival to complete successful response; also includes generator dispatch delay.",
		"vm_startup_ms":         "Fresh disk preparation and QEMU launch through guest READY; includes original background boot for pool hits.",
		"vm_boot_ms":            "QEMU launch through guest READY.",
	}
	allPassed := true
	stopReason := "all levels tested"
outer:
	for users := c.start; ; users = min(c.target, users*2) {
		for repetition := 1; repetition <= c.repetitions; repetition++ {
			fmt.Printf("Testing %d users at %.2f requests/s for %.0fs (run %d/%d)...\n", users, float64(users)*c.rate/60, c.duration, repetition, c.repetitions)
			r, err := stage(client, c, body, users)
			if err != nil {
				stopReason = err.Error()
				allPassed = false
				break outer
			}
			r["repetition"] = repetition
			stages = append(stages, r)
			allPassed = allPassed && r["passes_slo"].(bool)
			printStage(r, c.qualify)
			if c.verbose {
				display := map[string]any{}
				for k, v := range r {
					if k != "resource_timeline" {
						display[k] = v
					}
				}
				b, _ := json.MarshalIndent(display, "", "  ")
				fmt.Println(string(b))
			}
			if reason := rampStopReason(r); reason != "" {
				stopReason = reason
				break outer
			}
		}
		if users == c.target {
			break
		}
	}
	report["stages"] = stages
	report["stop_reason"] = stopReason
	report["qualified"] = c.qualify && allPassed && len(stages) == c.repetitions
	b, _ := json.MarshalIndent(report, "", "  ")
	if err = os.WriteFile(c.output, append(b, '\n'), 0600); err != nil {
		panic(err)
	}
	fmt.Printf("Final report: %s; qualified=%v; %s\n", c.output, report["qualified"], stopReason)
	if c.qualify && !report["qualified"].(bool) {
		os.Exit(1)
	}
}

func rampStopReason(r map[string]any) string {
	if !r["workers_stable"].(bool) || !r["server_drained"].(bool) || r["generator_dropped"].(int) > 0 {
		return "worker population, generator, or drain limit invalidated ramp"
	}
	return ""
}

func printStage(r map[string]any, qualify bool) {
	fmt.Printf("  Completed %d/%d | failures %.2f%% | prebooted guests %d\n", r["completed"], r["planned_requests"], r["error_fraction"].(float64)*100, r["pool_hits"])
	for _, item := range []struct{ key, label string }{
		{"request_completion_ms", "Request completion"},
		{"cpu_wait_ms", "CPU admission wait"},
		{"vm_startup_ms", "VM startup (incl. preboot)"},
		{"execution_ms", "Script execution"},
	} {
		if d, ok := r[item.key].(map[string]any); ok && d["samples"].(int) > 0 {
			fmt.Printf("  %-26s p50=%9.2f ms  p95=%9.2f ms\n", item.label, d["p50"], d["p95"])
		} else {
			fmt.Printf("  %-26s no successful samples\n", item.label)
		}
	}
	if qualify {
		fmt.Printf("  Meets qualification SLO: %v\n", r["passes_slo"])
	} else {
		fmt.Println("  Latency is reported only; exceeding the SLO does not stop the ramp.")
	}
}
