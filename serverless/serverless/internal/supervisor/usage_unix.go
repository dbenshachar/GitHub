//go:build linux || darwin

package supervisor

import (
	"os"
	"runtime"
	"syscall"
)

func peakRSS(p *os.ProcessState) int64 {
	r := p.SysUsage().(*syscall.Rusage).Maxrss
	if runtime.GOOS == "linux" {
		r *= 1024
	}
	return r
}
