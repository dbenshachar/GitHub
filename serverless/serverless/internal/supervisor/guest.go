package supervisor

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"time"
)

type Guest struct {
	memory  int
	cmd     *exec.Cmd
	input   io.WriteCloser
	frames  chan Frame
	done    chan struct{}
	stop    chan struct{}
	dir     string
	qmp     net.Conn
	decoder *json.Decoder
	qmu     sync.Mutex
	once    sync.Once
	started time.Time
	spawned time.Time
	ready   time.Time
	cpu     float64
	rss     int64
	err     error
}

func readFrame(r io.Reader) (Frame, error) {
	var h [5]byte
	if _, err := io.ReadFull(r, h[:]); err != nil {
		return Frame{}, err
	}
	n := binary.LittleEndian.Uint32(h[1:])
	if n > 90000 {
		return Frame{}, fmt.Errorf("oversized UART frame")
	}
	f := Frame{Kind: h[0], Body: make([]byte, n)}
	_, err := io.ReadFull(r, f.Body)
	return f, err
}
func bundle(s Spec) []byte {
	var b bytes.Buffer
	binary.Write(&b, binary.LittleEndian, uint32(len(s.Script)))
	b.WriteString(s.Script)
	for name, source := range s.Scripts {
		b.WriteByte(byte(len(name)))
		b.WriteString(name)
		binary.Write(&b, binary.LittleEndian, uint32(len(source)))
		b.WriteString(source)
	}
	return b.Bytes()
}
func (g *Guest) command(name string) error {
	g.qmu.Lock()
	defer g.qmu.Unlock()
	g.qmp.SetDeadline(time.Now().Add(2 * time.Second))
	defer g.qmp.SetDeadline(time.Time{})
	if err := json.NewEncoder(g.qmp).Encode(map[string]any{"execute": name}); err != nil {
		return err
	}
	for {
		var reply map[string]json.RawMessage
		if err := g.decoder.Decode(&reply); err != nil {
			return err
		}
		if _, ok := reply["error"]; ok {
			return fmt.Errorf("QMP %s: %s", name, reply["error"])
		}
		if _, ok := reply["return"]; ok {
			return nil
		}
	}
}
func (g *Guest) Close() {
	g.once.Do(func() {
		if g.cmd.Process != nil {
			close(g.stop)
			g.cmd.Process.Kill()
		}
		<-g.done
		g.input.Close()
		if g.qmp != nil {
			g.qmp.Close()
		}
		os.RemoveAll(g.dir)
	})
}
func boot(ctx context.Context, root, accel string, s Spec) (g *Guest, err error) {
	g = &Guest{memory: s.Memory, frames: make(chan Frame, 16), done: make(chan struct{}), stop: make(chan struct{}), started: time.Now()}
	g.dir, err = os.MkdirTemp("", "mini-guest-")
	if err != nil {
		return nil, err
	}
	disk := filepath.Join(g.dir, "disk.img")
	if err = blankDisk(disk); err != nil {
		os.RemoveAll(g.dir)
		return nil, err
	}
	cpu := "cortex-a57"
	if accel == "kvm" {
		cpu = "host"
	}
	args := []string{"-M", "virt", "-accel", accel, "-cpu", cpu, "-m", fmt.Sprint(s.Memory), "-smp", "1", "-nodefaults", "-nographic", "-monitor", "none", "-serial", "stdio", "-qmp", "unix:" + filepath.Join(g.dir, "qmp") + ",server=on,wait=off", "-kernel", filepath.Join(root, "kernel.elf"), "-drive", "if=none,file=" + disk + ",format=raw,id=disk", "-device", "virtio-blk-device,drive=disk", "-action", "shutdown=poweroff"}
	if s.Network {
		args = append(args, "-netdev", "user,id=net", "-device", "virtio-net-device,netdev=net")
	}
	g.cmd = exec.Command("qemu-system-aarch64", args...)
	var stderr bytes.Buffer
	g.cmd.Stderr = &stderr
	g.input, err = g.cmd.StdinPipe()
	if err != nil {
		os.RemoveAll(g.dir)
		return nil, err
	}
	stdout, e := g.cmd.StdoutPipe()
	if e != nil {
		os.RemoveAll(g.dir)
		return nil, e
	}
	g.spawned = time.Now()
	if err = g.cmd.Start(); err != nil {
		g.input.Close()
		os.RemoveAll(g.dir)
		return nil, err
	}
	readDone := make(chan struct{})
	go func() {
		<-readDone
		g.err = g.cmd.Wait()
		g.cpu = g.cmd.ProcessState.UserTime().Seconds() + g.cmd.ProcessState.SystemTime().Seconds()
		g.rss = peakRSS(g.cmd.ProcessState)
		close(g.done)
	}()
	go func() {
		defer close(readDone)
		r := bufio.NewReader(stdout)
		for {
			f, e := readFrame(r)
			if e != nil {
				close(g.frames)
				return
			}
			select {
			case g.frames <- f:
			case <-g.stop:
				return
			}
		}
	}()
	created := g
	defer func() {
		if err != nil {
			created.Close()
		}
	}()
	var f Frame
	select {
	case f = <-g.frames:
	case <-ctx.Done():
		return g, ctx.Err()
	}
	if f.Kind != 'R' {
		g.Close()
		return g, fmt.Errorf("guest failed before READY: %s", stderr.String())
	}
	g.ready = time.Now()
	for {
		g.qmp, err = net.DialTimeout("unix", filepath.Join(g.dir, "qmp"), time.Second)
		if err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return g, ctx.Err()
		case <-time.After(time.Millisecond):
		}
	}
	stopQMP := context.AfterFunc(ctx, func() { g.qmp.Close() })
	defer stopQMP()
	if err = g.readGreeting(ctx); err != nil {
		return g, err
	}
	if err = g.command("qmp_capabilities"); err != nil {
		return g, err
	}
	if err = g.command("stop"); err != nil {
		return g, err
	}
	// stop's reply confirms the vCPUs have stopped; verify status independently.
	g.qmu.Lock()
	g.qmp.SetDeadline(time.Now().Add(time.Second))
	json.NewEncoder(g.qmp).Encode(map[string]any{"execute": "query-status"})
	for {
		var v map[string]json.RawMessage
		if err = g.decoder.Decode(&v); err != nil {
			break
		}
		if raw, ok := v["return"]; ok {
			var status struct{ Running bool }
			err = json.Unmarshal(raw, &status)
			if status.Running {
				err = fmt.Errorf("guest did not pause")
			}
			break
		}
	}
	g.qmp.SetDeadline(time.Time{})
	g.qmu.Unlock()
	if err != nil {
		return g, err
	}
	return g, nil
}
func (g *Guest) Run(ctx context.Context, s Spec) (string, []Fanout, map[string]any, error) {
	if err := ctx.Err(); err != nil {
		return "", nil, nil, err
	}
	stopIO := context.AfterFunc(ctx, func() { g.qmp.Close(); g.input.Close() })
	defer stopIO()
	began := time.Now()
	if err := g.command("cont"); err != nil {
		return "", nil, nil, err
	}
	b := bundle(s)
	h := []byte{'S', 0, 0, 0, 0}
	binary.LittleEndian.PutUint32(h[1:], uint32(len(b)))
	writeDone := make(chan error, 1)
	go func() { _, err := g.input.Write(append(h, b...)); writeDone <- err }()
	select {
	case err := <-writeDone:
		if err != nil {
			return "", nil, nil, err
		}
	case <-ctx.Done():
		return "", nil, nil, ctx.Err()
	}
	var out bytes.Buffer
	var children []Fanout
	started := time.Now()
	totalChildren := 0
	for {
		select {
		case <-ctx.Done():
			return "", nil, nil, ctx.Err()
		case f, ok := <-g.frames:
			if !ok {
				return "", nil, nil, fmt.Errorf("guest exited without DONE")
			}
			switch f.Kind {
			case 'B':
				started = time.Now()
			case 'O':
				if out.Len()+len(f.Body) > 65536 {
					return "", nil, nil, fmt.Errorf("guest output exceeds 64 KiB")
				}
				out.Write(f.Body)
			case 'F':
				if len(f.Body) < 5 {
					return "", nil, nil, fmt.Errorf("invalid fanout frame")
				}
				n := int(binary.LittleEndian.Uint32(f.Body))
				name := string(f.Body[4:])
				totalChildren += n
				if n < 1 || totalChildren > 256 || !filename.MatchString(name) {
					return "", nil, nil, fmt.Errorf("fanout limit exceeded")
				}
				if _, ok := s.Scripts[name]; !ok {
					return "", nil, nil, fmt.Errorf("unknown child script")
				}
				children = append(children, Fanout{name, n})
			case 'D':
				if string(f.Body) != "0" {
					return "", nil, nil, fmt.Errorf("Mini OS script failed: %s", out.String())
				}
				finished := time.Now()
				select {
				case <-g.done:
				case <-ctx.Done():
					return "", nil, nil, ctx.Err()
				}
				metrics := map[string]any{"startup_ms": g.ready.Sub(g.started).Seconds() * 1000,
					"vm_startup_ms": g.ready.Sub(g.started).Seconds() * 1000,
					"vm_boot_ms":    g.ready.Sub(g.spawned).Seconds() * 1000,
					"disk_setup_ms": g.spawned.Sub(g.started).Seconds() * 1000,
					"execution_ms":  finished.Sub(started).Seconds() * 1000, "assignment_ms": started.Sub(began).Seconds() * 1000, "lifecycle_cpu_ms": g.cpu * 1000, "peak_rss_bytes": g.rss}
				return out.String(), children, metrics, nil
			default:
				return "", nil, nil, fmt.Errorf("unknown guest frame")
			}
		}
	}
}

func (g *Guest) readGreeting(ctx context.Context) error {
	stop := context.AfterFunc(ctx, func() { g.qmp.Close() })
	defer stop()
	deadline := time.Now().Add(2 * time.Second)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	g.qmp.SetDeadline(deadline)
	defer g.qmp.SetDeadline(time.Time{})
	g.decoder = json.NewDecoder(g.qmp)
	var greeting map[string]any
	return g.decoder.Decode(&greeting)
}
