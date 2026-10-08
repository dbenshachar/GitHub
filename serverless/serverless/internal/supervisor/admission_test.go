package supervisor

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"
)

func queuedEngine() *Engine {
	return &Engine{cfg: Config{CPUs: 1, MemoryMB: 512}, ctx: context.Background(), cost: .1, peak: 40 * 1024 * 1024, last: time.Now(), cpuUsed: 1, guests: map[*Guest]*reservation{}}
}
func waitQueue(t *testing.T, e *Engine, n int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		e.mu.Lock()
		size := e.cpuWaiters.Len()
		e.mu.Unlock()
		if size == n {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("did not reach %d waiting requests", n)
}
func TestCPUAdmissionWaitsThenReserves(t *testing.T) {
	e := queuedEngine()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, res, _, waited, err := e.awaitAdmission(ctx, Spec{Memory: 8, CPUs: 1})
		if err == nil && (res == nil || waited < 20*time.Millisecond) {
			err = errors.New("waiting admission did not reserve after recovery")
		}
		done <- err
	}()
	waitQueue(t, e, 1)
	e.mu.Lock()
	if e.reserved != 0 || e.booting != 0 || e.rejected != 0 {
		t.Fatal("waiting request consumed guest resources or was rejected")
	}
	e.mu.Unlock()
	select {
	case err := <-done:
		t.Fatal("request did not stall", err)
	case <-time.After(25 * time.Millisecond):
	}
	e.mu.Lock()
	e.cpuUsed = 0
	e.credits = .2
	e.wakeCPULocked()
	e.mu.Unlock()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	waitQueue(t, e, 0)
}
func TestCancelledCPUWaitDoesNotReserve(t *testing.T) {
	e := queuedEngine()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, _, _, _, err := e.awaitAdmission(ctx, Spec{Memory: 8, CPUs: 1}); done <- err }()
	waitQueue(t, e, 1)
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	waitQueue(t, e, 0)
	if e.reserved != 0 || e.booting != 0 || e.rejected != 0 {
		t.Fatal("cancelled waiter leaked reservation")
	}
}
func TestCPUWaitersAreFIFOAndPrewarmYields(t *testing.T) {
	e := queuedEngine()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	order := make(chan int, 2)
	for i := 1; i <= 2; i++ {
		go func(i int) {
			_, _, _, _, err := e.awaitAdmission(ctx, Spec{Memory: 8, CPUs: 1})
			if err != nil {
				order <- -i
			} else {
				order <- i
			}
		}(i)
		waitQueue(t, e, i)
	}
	if _, err := e.reserve(8); err == nil {
		t.Fatal("background prewarm took foreground admission")
	}
	e.mu.Lock()
	e.cpuUsed = 0
	e.credits = e.cost
	e.wakeCPULocked()
	e.mu.Unlock()
	if first := <-order; first != 1 {
		t.Fatal("waiters reordered", first)
	}
	waitQueue(t, e, 1)
	e.mu.Lock()
	e.credits = e.cost
	e.wakeCPULocked()
	e.mu.Unlock()
	if second := <-order; second != 2 {
		t.Fatal("second waiter did not recover", second)
	}
	waitQueue(t, e, 0)
}
func TestExpiredCPUWaitReportsDeadline(t *testing.T) {
	e := queuedEngine()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, _, _, _, err := e.awaitAdmission(ctx, Spec{Memory: 8, CPUs: 1})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("deadline changed into CPU rejection", err)
	}
	waitQueue(t, e, 0)
}
func TestCPUCreditCapacityAllowsExpensiveSingleJob(t *testing.T) {
	e := queuedEngine()
	e.cpuUsed = 0
	e.cost = 2
	e.last = time.Now().Add(-10 * time.Second)
	e.mu.Lock()
	e.replenishLocked(time.Now())
	available := e.cpuAvailableLocked(false)
	e.mu.Unlock()
	if !available {
		t.Fatal("job could never recover enough credits")
	}
}
func TestRealCPUWaitRecoversAndRunsGuest(t *testing.T) {
	root := os.Getenv("MINI_RUNTIME_V2")
	if root == "" {
		t.Skip("set MINI_RUNTIME_V2")
	}
	e, err := NewEngine(context.Background(), Config{Root: root, Accel: "tcg", CPUs: 3, MemoryMB: 512, PoolMin: 0, PoolMax: 0, CPUCost: .01})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	e.mu.Lock()
	e.credits = -.2
	e.last = time.Now()
	e.mu.Unlock()
	r, err := e.Invoke(context.Background(), Spec{Memory: 8, CPUs: 1, Timeout: 5, Script: "print(42);"}, 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	if r.Output != "42\n" || r.Metrics["cpu_wait_ms"].(float64) < 50 {
		t.Fatal("CPU budget was not awaited", r)
	}
}
