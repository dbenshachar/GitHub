package supervisor

import (
	"bytes"
	"context"
	"encoding/binary"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestProtocolBounds(t *testing.T) {
	h := []byte{'O', 0, 0, 0, 0}
	binary.LittleEndian.PutUint32(h[1:], 90001)
	if _, err := readFrame(bytes.NewReader(h)); err == nil {
		t.Fatal("oversize accepted")
	}
	if _, err := readFrame(bytes.NewReader([]byte{'D'})); err == nil {
		t.Fatal("truncation accepted")
	}
}
func TestFreshSparseDisk(t *testing.T) {
	p := filepath.Join(t.TempDir(), "disk")
	if err := blankDisk(p); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if len(b) != 16*1024*1024 || b[510] != 0x55 || b[511] != 0xaa {
		t.Fatal("bad FAT32 geometry")
	}
	if binary.LittleEndian.Uint32(b[32*512+8:]) != 0xfffffff {
		t.Fatal("root not terminated")
	}
	if err := blankDisk(p); err == nil {
		t.Fatal("existing volume overwritten")
	}
}
func TestValidation(t *testing.T) {
	for _, s := range []Spec{{Script: "\x00"}, {Memory: 7}, {Timeout: 601}, {WarmRuns: 1}, {Scripts: map[string]string{"../X.MOS": ""}}} {
		if s.Validate() == nil {
			t.Fatalf("invalid spec accepted %+v", s)
		}
	}
}
func TestCron(t *testing.T) {
	f, err := cronFields("*/15 0-23 * * 1,3")
	if err != nil || !f[0][45] || f[0][46] || !f[4][3] {
		t.Fatal("bad cron", err)
	}
	if _, err = cronFields("60 * * * *"); err == nil {
		t.Fatal("bad minute accepted")
	}
}
func TestRealGuest(t *testing.T) {
	root := os.Getenv("MINI_RUNTIME_V2")
	if root == "" {
		t.Skip("set MINI_RUNTIME_V2 for real QEMU tests")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	s := Spec{Script: "print(42); fanout(\"CHILD.MOS\", 2);", Scripts: map[string]string{"CHILD.MOS": "print(1);"}, Memory: 8, Timeout: 10}
	g, err := boot(ctx, root, "tcg", s)
	if err != nil {
		t.Fatal(err)
	}
	out, children, m, err := g.Run(ctx, s)
	g.Close()
	if err != nil || out != "42\n" || len(children) != 1 || children[0].Count != 2 {
		t.Fatalf("result %q %+v %+v %v", out, children, m, err)
	}
	if g.cpu <= 0 {
		t.Fatal("lifecycle CPU missing")
	}
	startup := m["vm_startup_ms"].(float64)
	bootTime := m["vm_boot_ms"].(float64)
	if startup <= 0 || bootTime <= 0 || bootTime > startup {
		t.Fatal("invalid VM startup measurement", m)
	}
	s.Script = "shell(\"not_a_command\");"
	g, err = boot(ctx, root, "tcg", s)
	if err != nil {
		t.Fatal(err)
	}
	_, _, _, err = g.Run(ctx, s)
	g.Close()
	if err == nil {
		t.Fatal("failed script succeeded")
	}
}
func TestRealPoolIsolation(t *testing.T) {
	root := os.Getenv("MINI_RUNTIME_V2")
	if root == "" {
		t.Skip("set MINI_RUNTIME_V2")
	}
	e, err := NewEngine(context.Background(), Config{Root: root, Accel: "tcg", CPUs: 3, MemoryMB: 512, PoolMin: 1, PoolMax: 2, CPUCost: .01})
	if err != nil {
		t.Fatal(err)
	}
	defer e.Close()
	time.Sleep(400 * time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	r, err := e.Invoke(ctx, Spec{Script: "print(1);", Memory: 8}, 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	if r.Metrics["pool_hit"] != true {
		t.Fatal("unused guest not assigned", r.Metrics)
	}
	r2, err := e.Invoke(ctx, Spec{Script: "print(2);", Memory: 8}, 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	if r.ID == r2.ID || r2.Output != "2\n" {
		t.Fatal("guest reused")
	}
}

func TestRealFanoutAndCancellation(t *testing.T) {
	root := os.Getenv("MINI_RUNTIME_V2")
	if root == "" {
		t.Skip("set MINI_RUNTIME_V2")
	}
	ctx, cancel := context.WithCancel(context.Background())
	e, err := NewEngine(ctx, Config{Root: root, Accel: "tcg", CPUs: 3, MemoryMB: 512, PoolMin: 0, PoolMax: 0, CPUCost: .001})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	s := Spec{Script: `fanout(2, "CHILD.MOS");`, Scripts: map[string]string{"CHILD.MOS": "print(7);"}, Memory: 8, Timeout: 10}
	r, err := e.Invoke(ctx, s, 1, 0)
	if err != nil {
		cancel()
		e.Close()
		t.Fatal(err)
	}
	if len(r.Children) != 2 || r.Children[0].Output != "7\n" || r.Children[0].ID == r.Children[1].ID {
		t.Fatal("independent children missing")
	}
	cancel()
	e.Close()
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.reserved != 0 || len(e.guests) != 0 {
		t.Fatal("guest resources leaked on shutdown")
	}
}

func TestStalledQMPGreetingIsBounded(t *testing.T) {
	server, client := net.Pipe()
	defer server.Close()
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	g := &Guest{qmp: client}
	start := time.Now()
	if err := g.readGreeting(ctx); err == nil {
		t.Fatal("stalled greeting accepted")
	}
	if time.Since(start) > 250*time.Millisecond {
		t.Fatal("greeting ignored cancellation")
	}
}
