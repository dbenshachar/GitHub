//go:build linux || darwin

package supervisor

import (
	"os"
	"runtime"
	"syscall"
)

func fdHeadroom() bool {
	var r syscall.Rlimit
	if syscall.Getrlimit(syscall.RLIMIT_NOFILE, &r) != nil {
		return false
	}
	fds, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		fds, err = os.ReadDir("/dev/fd")
	}
	if err != nil && runtime.GOOS == "darwin" {
		return r.Cur >= 256
	}
	return err == nil && uint64(len(fds)+32) < r.Cur
}
