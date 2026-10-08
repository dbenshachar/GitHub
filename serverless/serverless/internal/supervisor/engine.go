package supervisor

import (
	"container/list"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type Config struct {
	AccelReason             string
	Root, Accel, Name, Node string
	CPUs                    float64
	MemoryMB                int
	PoolMin, PoolMax        int
	CPUCost                 float64
	Dispatcher              func(context.Context, Spec, int) (*Result, error)
	Calibrate               int
}
type idleGuest struct {
	guest       *Guest
	expires     time.Time
	reservation *reservation
}
type reservation struct {
	bytes   int64
	charged float64
}
type cpuWaiter struct {
	ready    chan struct{}
	notified bool
	pooled   bool
}
type Engine struct {
	cpuWaiters                  list.List
	bootID                      string
	finishedCPU                 float64
	cfg                         Config
	guests                      map[*Guest]*reservation
	cpuSample                   float64
	cpuSampleAt                 time.Time
	cpuUsed                     float64
	mu                          sync.Mutex
	idle                        []idleGuest
	reserved                    int64
	active, booting             int
	credits, cost, peak         float64
	last                        time.Time
	arrival                     float64
	lastArrival                 time.Time
	cold                        float64
	completed, rejected, misses uint64
	closed                      bool
	cancel                      context.CancelFunc
	ctx                         context.Context
	wg                          sync.WaitGroup
}

func NewEngine(ctx context.Context, c Config) (*Engine, error) {
	if _, err := os.Stat(c.Root + "/job-runtime-v2"); err != nil {
		return nil, fmt.Errorf("build the framed runtime with --protocol 2: %w", err)
	}
	if math.IsNaN(c.CPUs) || math.IsInf(c.CPUs, 0) || c.CPUs <= 0 || c.MemoryMB < 64 || c.PoolMin < 0 || c.PoolMax < c.PoolMin {
		return nil, fmt.Errorf("invalid supervisor budgets")
	}
	if c.Name == "" {
		c.Name = "standalone"
	}
	if c.Node == "" {
		c.Node, _ = os.Hostname()
	}
	probe := func(accel string) error {
		pc, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		g, err := boot(pc, c.Root, accel, Spec{Memory: 8})
		if err != nil {
			return err
		}
		defer g.Close()
		_, _, _, err = g.Run(pc, Spec{Script: "print(1);", Memory: 8})
		return err
	}
	if c.Accel == "auto" {
		c.Accel = "tcg"
		c.AccelReason = "/dev/kvm unavailable"
		if _, err := os.Stat("/dev/kvm"); err == nil {
			if err := probe("kvm"); err == nil {
				c.Accel = "kvm"
				c.AccelReason = "real Mini OS KVM smoke passed"
			} else {
				c.AccelReason = "KVM smoke failed: " + err.Error()
			}
		}
	} else if c.Accel != "tcg" && c.Accel != "kvm" {
		return nil, fmt.Errorf("accel must be auto, tcg, or kvm")
	}
	if err := probe(c.Accel); err != nil {
		return nil, fmt.Errorf("%s smoke test: %w", c.Accel, err)
	}
	e := &Engine{bootID: id(), guests: make(map[*Guest]*reservation), cfg: c, cost: c.CPUCost, peak: 40 * 1024 * 1024, last: time.Now(), cold: .1}
	if e.cost <= 0 {
		e.cost = .1
	}
	e.ctx, e.cancel = context.WithCancel(ctx)
	// Calibration uses full cold lifetimes, including disk setup and guest shutdown.
	var cpuSamples, bootSamples []float64
	for i := 0; i < c.Calibrate; i++ {
		pc, cancel := context.WithTimeout(ctx, 10*time.Second)
		g, err := boot(pc, c.Root, c.Accel, Spec{Memory: 8})
		if err == nil {
			_, _, _, err = g.Run(pc, Spec{Script: "print(1);", Memory: 8})
			g.Close()
		}
		cancel()
		if err != nil {
			e.cancel()
			return nil, fmt.Errorf("calibration: %w", err)
		}
		cpuSamples = append(cpuSamples, g.cpu)
		e.peak = math.Max(e.peak, math.Max(32*1024*1024, float64(g.rss)-float64(g.memory-8)*1024*1024)*1.2)
		bootSamples = append(bootSamples, g.ready.Sub(g.started).Seconds())
	}
	if len(cpuSamples) > 0 {
		sort.Float64s(cpuSamples)
		sort.Float64s(bootSamples)
		i := int(math.Ceil(float64(len(cpuSamples))*.95)) - 1
		e.cost = math.Max(.001, cpuSamples[i])
		e.cold = bootSamples[i]
	}
	e.credits = c.CPUs * .7 // one second burst; booting and assigned guests share these credits.
	e.wg.Add(1)
	go e.refill()
	initial := min(c.PoolMin, int(float64(c.MemoryMB)*1024*1024/e.peak), int(1024*1024*1024/e.peak))
	deadline := time.Now().Add(10 * time.Second)
	for initial > 0 && time.Now().Before(deadline) {
		e.mu.Lock()
		ready := len(e.idle) >= initial
		e.mu.Unlock()
		if ready {
			break
		}
		select {
		case <-ctx.Done():
			e.Close()
			return nil, ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
	return e, nil
}
func id() string { b := make([]byte, 16); rand.Read(b); return hex.EncodeToString(b) }

// Background preparation never waits and yields to foreground admissions.
func (e *Engine) reserve(memory int) (*reservation, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.cpuWaiters.Len() > 0 {
		return nil, &Rejection{Reason: "foreground admission waiting"}
	}
	return e.reserveLocked(memory)
}
func (e *Engine) replenishLocked(now time.Time) {
	capacity := math.Max(e.cfg.CPUs*.7, e.cost)
	e.credits = math.Min(capacity, e.credits+now.Sub(e.last).Seconds()*e.cfg.CPUs*.7)
	e.last = now
}
func (e *Engine) cpuAvailableLocked(pooled bool) bool {
	if e.cpuUsed > e.cfg.CPUs*.85 {
		return false
	}
	if pooled && len(e.idle) > 0 {
		return e.credits >= 0
	}
	return e.credits >= e.cost
}
func (e *Engine) wakeCPULocked() {
	if head := e.cpuWaiters.Front(); head != nil {
		w := head.Value.(*cpuWaiter)
		if !w.notified && (e.closed || e.cpuAvailableLocked(w.pooled)) {
			w.notified = true
			close(w.ready)
		}
	}
}
func (e *Engine) reserveLocked(memory int) (*reservation, error) {
	if e.closed {
		return nil, &Rejection{Reason: "worker shutting down"}
	}
	e.replenishLocked(time.Now())
	bytesNeeded := int64(e.peak) + int64(memory-8)*1024*1024
	if e.reserved+bytesNeeded > int64(e.cfg.MemoryMB)*1024*1024 || !liveMemory(bytesNeeded) {
		return nil, &Rejection{Reason: "memory headroom exhausted"}
	}
	if !e.cpuAvailableLocked(false) {
		return nil, &Rejection{Reason: "CPU credits exhausted"}
	}
	if !fdHeadroom() {
		return nil, &Rejection{Reason: "file descriptor headroom exhausted"}
	}
	e.credits -= e.cost
	e.reserved += bytesNeeded
	e.booting++
	return &reservation{bytes: bytesNeeded, charged: e.cost}, nil
}

// FIFO waiters allocate no VM or RAM reservation until CPU becomes available.
// Only the head is notified; this avoids waking every pending request each tick.
func (e *Engine) awaitAdmission(ctx context.Context, s Spec) (*Guest, *reservation, bool, time.Duration, error) {
	began := time.Now()
	pooled := s.Memory == 8 && s.CPUs == 1 && !s.Network
	var element *list.Element
	e.mu.Lock()
	defer func() {
		if element != nil {
			e.cpuWaiters.Remove(element)
			e.wakeCPULocked()
		}
		e.mu.Unlock()
	}()
	for {
		if err := ctx.Err(); err != nil {
			return nil, nil, false, time.Since(began), err
		}
		if e.closed {
			return nil, nil, false, time.Since(began), context.Canceled
		}
		e.replenishLocked(time.Now())
		atHead := e.cpuWaiters.Len() == 0 || (element != nil && e.cpuWaiters.Front() == element)
		if atHead && e.cpuAvailableLocked(pooled) {
			if pooled && len(e.idle) > 0 {
				v := e.idle[len(e.idle)-1]
				e.idle = e.idle[:len(e.idle)-1]
				return v.guest, v.reservation, true, time.Since(began), nil
			}
			res, err := e.reserveLocked(s.Memory)
			if err != nil {
				e.rejected++
				return nil, nil, false, time.Since(began), err
			}
			e.misses++
			return nil, res, false, time.Since(began), nil
		}
		if element == nil {
			element = e.cpuWaiters.PushBack(&cpuWaiter{ready: make(chan struct{}), pooled: pooled})
		}
		waiter := element.Value.(*cpuWaiter)
		if waiter.notified {
			waiter.ready = make(chan struct{})
			waiter.notified = false
		}
		ready := waiter.ready
		e.wakeCPULocked()
		e.mu.Unlock()
		select {
		case <-ready:
		case <-ctx.Done():
		case <-e.ctx.Done():
		}
		e.mu.Lock()
	}
}
func (e *Engine) release(g *Guest, reservation *reservation) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.reserved -= reservation.bytes
	delete(e.guests, g)
	e.finishedCPU += g.cpu
	e.credits += reservation.charged - g.cpu
	if g.rss > 0 {
		e.peak = math.Max(e.peak, math.Max(32*1024*1024, float64(g.rss)-float64(g.memory-8)*1024*1024)*1.2)
	}
	e.cost = .95*e.cost + .05*math.Max(g.cpu, .001)
	e.wakeCPULocked()
}
func (e *Engine) acquire(ctx context.Context, s Spec) (*Guest, *reservation, bool, time.Duration, error) {
	e.mu.Lock()
	e.active++
	now := time.Now()
	if !e.lastArrival.IsZero() {
		rate := 1 / math.Max(now.Sub(e.lastArrival).Seconds(), .001)
		e.arrival = .95*e.arrival + .05*rate
	}
	e.lastArrival = now
	e.mu.Unlock()
	g, res, warm, waited, err := e.awaitAdmission(ctx, s)
	if err != nil || warm {
		return g, res, warm, waited, err
	}
	bc, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	g, err = boot(bc, e.cfg.Root, e.cfg.Accel, s)
	e.mu.Lock()
	e.booting--
	if err != nil {
		if g != nil {
			e.credits += res.charged - g.cpu
			e.finishedCPU += g.cpu
		}
		e.reserved -= res.bytes
		e.wakeCPULocked()
		e.mu.Unlock()
		return nil, nil, false, waited, err
	}
	e.guests[g] = res
	e.mu.Unlock()
	return g, res, false, waited, nil
}
func (e *Engine) Invoke(ctx context.Context, s Spec, count, depth int) (*Result, error) {
	e.mu.Lock()
	if e.closed {
		e.mu.Unlock()
		return nil, &Rejection{Reason: "worker shutting down"}
	}
	e.wg.Add(1)
	e.mu.Unlock()
	defer e.wg.Done()
	if err := s.Validate(); err != nil {
		return nil, err
	}
	if depth < 0 || depth > 8 || count < 1 || count > 256 {
		return nil, fmt.Errorf("fanout/depth limit exceeded")
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.Timeout)*time.Second)
	stop := context.AfterFunc(e.ctx, cancel)
	defer stop()
	defer cancel()
	began := time.Now()
	if count > 1 {
		r := &Result{ID: id(), Metrics: map[string]any{}}
		children, err := e.parallel(ctx, s, count, depth)
		r.Children = children
		r.Metrics["fanout_ms"] = time.Since(began).Seconds() * 1000
		if err != nil {
			err = postAdmission(err)
		}
		return r, err
	}
	g, res, warm, waited, err := e.acquire(ctx, s)
	defer func() { e.mu.Lock(); e.active--; e.mu.Unlock() }()
	if err != nil {
		return nil, err
	}
	out, fanouts, metrics, err := g.Run(ctx, s)
	g.Close()
	e.release(g, res)
	if err != nil {
		return nil, err
	}
	if warm {
		metrics["cold_startup_ms"] = metrics["startup_ms"]
		metrics["startup_ms"] = metrics["assignment_ms"]
	}
	metrics["cpu_wait_ms"] = waited.Seconds() * 1000
	metrics["pool_hit"] = warm
	metrics["acceleration"] = e.cfg.Accel
	metrics["worker_node"] = e.cfg.Node
	metrics["worker_name"] = e.cfg.Name
	r := &Result{ID: id(), Output: out, Metrics: metrics}
	// Parent is destroyed and its reservation released before scheduling children.
	for _, f := range fanouts {
		child := s
		child.Script = s.Scripts[f.Name]
		children, err := e.parallel(ctx, child, f.Count, depth+1)
		if err != nil {
			return nil, postAdmission(err)
		}
		r.Children = append(r.Children, children...)
	}
	metrics["total_ms"] = time.Since(began).Seconds() * 1000
	e.mu.Lock()
	e.completed++
	e.mu.Unlock()
	return r, nil
}
func (e *Engine) parallel(ctx context.Context, s Spec, n, depth int) ([]*Result, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	r := make([]*Result, n)
	var wg sync.WaitGroup
	var once sync.Once
	var failure error
	for i := range r {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			var err error
			if e.cfg.Dispatcher != nil {
				r[i], err = e.cfg.Dispatcher(ctx, s, depth)
			} else {
				r[i], err = e.Invoke(ctx, s, 1, depth)
			}
			if err != nil {
				once.Do(func() { failure = err; cancel() })
			}
		}(i)
	}
	wg.Wait()
	return r, failure
}
func (e *Engine) refill() {
	defer e.wg.Done()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-e.ctx.Done():
			return
		case <-ticker.C:
			e.mu.Lock()
			var expired []idleGuest
			now := time.Now()
			e.sampleCPU(now)
			e.replenishLocked(now)
			e.wakeCPULocked()
			kept := e.idle[:0]
			for _, v := range e.idle {
				if now.After(v.expires) {
					expired = append(expired, v)
				} else {
					kept = append(kept, v)
				}
			}
			e.idle = kept
			target := int(math.Ceil(e.arrival * e.cold))
			target = max(e.cfg.PoolMin, min(e.cfg.PoolMax, target))
			target = min(target, int(1024*1024*1024/e.peak))
			availableCredits := math.Min(e.cfg.CPUs*.7, e.credits+now.Sub(e.last).Seconds()*e.cfg.CPUs*.7)
			needed := e.cpuWaiters.Len() == 0 && len(e.idle)+e.booting < target && e.booting < 2 && availableCredits >= 2*e.cost
			e.mu.Unlock()
			for _, v := range expired {
				v.guest.Close()
				e.release(v.guest, v.reservation)
			}
			if !needed {
				continue
			}
			res, err := e.reserve(8)
			if err != nil {
				continue
			}
			e.wg.Add(1)
			go func(res *reservation) {
				defer e.wg.Done()
				bootStarted := time.Now()
				bc, cancel := context.WithTimeout(e.ctx, 10*time.Second)
				g, err := boot(bc, e.cfg.Root, e.cfg.Accel, Spec{Memory: 8})
				cancel()
				e.mu.Lock()
				e.booting--
				if err == nil && !e.closed {
					e.guests[g] = res
					if time.Since(bootStarted).Seconds() > e.cold {
						e.cold = .95*e.cold + .05*time.Since(bootStarted).Seconds()
					}
					e.idle = append(e.idle, idleGuest{g, time.Now().Add(60 * time.Second), res})
					e.wakeCPULocked()
					e.mu.Unlock()
				} else {
					e.mu.Unlock()
					if g != nil {
						g.Close()
						e.release(g, res)
					} else {
						e.mu.Lock()
						e.reserved -= res.bytes
						e.mu.Unlock()
					}
				}
			}(res)
		}
	}
}
func (e *Engine) Close() {
	e.mu.Lock()
	e.closed = true
	e.wakeCPULocked()
	e.mu.Unlock()
	e.cancel()
	e.wg.Wait()
	e.mu.Lock()
	idle := e.idle
	e.idle = nil
	e.mu.Unlock()
	for _, v := range idle {
		v.guest.Close()
		e.release(v.guest, v.reservation)
	}
}
func (e *Engine) Metrics() map[string]any {
	e.mu.Lock()
	defer e.mu.Unlock()
	w := map[string]any{"boot_id": e.bootID, "acceleration_reason": e.cfg.AccelReason, "cpu_used": e.cpuUsed, "cpu_waiting": e.cpuWaiters.Len(), "cgroup_cpu_stat": cpuStat(), "name": e.cfg.Name, "node": e.cfg.Node, "ready": !e.closed, "active": e.active, "cpu_budget": e.cfg.CPUs, "memory_budget_mb": e.cfg.MemoryMB, "reserved_bytes": e.reserved, "pool_idle": len(e.idle), "pool_misses": e.misses, "booting": e.booting, "cpu_cost_ms": e.cost * 1000, "cpu_credits": e.credits, "completed": e.completed, "rejected": e.rejected, "acceleration": e.cfg.Accel}
	return map[string]any{"backend": "go-supervisor", "pending": e.active, "workers": []any{w}}
}
func readInt(path string) int64 {
	b, err := os.ReadFile(path)
	if err != nil {
		return -1
	}
	n, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	if err != nil {
		return -1
	}
	return n
}
func liveMemory(needed int64) bool {
	limit := readInt(cgroupFile("memory.max"))
	used := readInt(cgroupFile("memory.current"))
	if limit > 0 && used >= 0 && limit-used < needed+128*1024*1024 {
		return false
	}
	b, err := os.ReadFile("/proc/meminfo")
	if err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "MemAvailable:") {
				fields := strings.Fields(line)
				v, _ := strconv.ParseInt(fields[1], 10, 64)
				return v*1024 > needed+256*1024*1024
			}
		}
	}
	return true
}

func postAdmission(err error) error {
	var r *Rejection
	if errors.As(err, &r) {
		return &Rejection{Reason: r.Reason, Admitted: true}
	}
	return err
}

// Called with the admission lock held. Charge overruns while guests are live;
// shutdown reconciles the exact charged amount, avoiding EWMA-based refunds.
func (e *Engine) sampleCPU(now time.Time) {
	total := e.finishedCPU
	for g, r := range e.guests {
		cpu := processCPU(g.cmd.Process.Pid)
		if cpu < 0 {
			continue
		}
		total += cpu
		if cpu > r.charged {
			e.credits -= cpu - r.charged
			r.charged = cpu
		}
	}
	if !e.cpuSampleAt.IsZero() && now.Sub(e.cpuSampleAt) >= 100*time.Millisecond {
		// Completed guests leave this sum, so per-process overruns are authoritative.
		e.cpuUsed = math.Max(0, (total-e.cpuSample)/now.Sub(e.cpuSampleAt).Seconds())
		e.cpuSample = total
		e.cpuSampleAt = now
	} else if e.cpuSampleAt.IsZero() {
		e.cpuSample = total
		e.cpuSampleAt = now
	}
}
func processCPU(pid int) float64 {
	b, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return -1
	}
	end := strings.LastIndex(string(b), ")")
	if end < 0 {
		return -1
	}
	fields := strings.Fields(string(b)[end+1:])
	if len(fields) < 13 {
		return -1
	}
	u, _ := strconv.ParseFloat(fields[11], 64)
	s, _ := strconv.ParseFloat(fields[12], 64)
	return (u + s) / 100
}

func cgroupFile(name string) string {
	b, err := os.ReadFile("/proc/self/cgroup")
	if err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			if strings.HasPrefix(line, "0::") {
				return "/sys/fs/cgroup" + strings.TrimPrefix(line, "0::") + "/" + name
			}
		}
	}
	return "/sys/fs/cgroup/" + name
}

func cpuStat() map[string]int64 {
	b, err := os.ReadFile(cgroupFile("cpu.stat"))
	r := map[string]int64{}
	if err == nil {
		for _, line := range strings.Split(string(b), "\n") {
			f := strings.Fields(line)
			if len(f) == 2 {
				v, _ := strconv.ParseInt(f[1], 10, 64)
				r[f[0]] = v
			}
		}
	}
	return r
}
