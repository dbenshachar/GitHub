package supervisor

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strings"
)

type Spec struct {
	Script   string            `json:"script"`
	Scripts  map[string]string `json:"scripts"`
	Memory   int               `json:"memory_mb"`
	CPUs     float64           `json:"cpus"`
	Timeout  int               `json:"timeout_s"`
	WarmRuns int               `json:"benchmark_warm_runs"`
	Network  bool              `json:"network"`
}

var filename = regexp.MustCompile(`^[A-Z0-9_-]{1,8}\.MOS$`)

func (s *Spec) Validate() error {
	if s.Memory == 0 {
		s.Memory = 128
	}
	if s.CPUs == 0 {
		s.CPUs = 1
	}
	if s.Timeout == 0 {
		s.Timeout = 600
	}
	if s.Memory < 8 || s.Memory > 4096 || math.IsNaN(s.CPUs) || math.IsInf(s.CPUs, 0) || s.CPUs <= 0 || s.CPUs > 64 || s.Timeout < 1 || s.Timeout > 600 {
		return fmt.Errorf("invalid guest resources or deadline")
	}
	if s.WarmRuns != 0 {
		return fmt.Errorf("warm probes on used guests are unsupported; each invocation is single-use")
	}
	if len(s.Scripts) > 12 {
		return fmt.Errorf("too many child scripts")
	}
	for name, source := range s.Scripts {
		if !filename.MatchString(name) || name == "RUN.MOS" {
			return fmt.Errorf("invalid child filename")
		}
		if len(source) > 60000 || strings.ContainsRune(source, 0) {
			return fmt.Errorf("invalid child source")
		}
	}
	if len(s.Script) > 60000 || strings.ContainsRune(s.Script, 0) {
		return fmt.Errorf("invalid source")
	}
	b, _ := json.Marshal(s)
	if len(b) > 90000 {
		return fmt.Errorf("bundle exceeds 90000 bytes")
	}
	return nil
}

type Result struct {
	ID       string         `json:"invocation_id"`
	Output   string         `json:"output"`
	Metrics  map[string]any `json:"metrics"`
	Children []*Result      `json:"children,omitempty"`
}
type Rejection struct {
	Reason   string
	Admitted bool
}

func (e *Rejection) Error() string { return e.Reason }

type Frame struct {
	Kind byte
	Body []byte
}
type Fanout struct {
	Name  string
	Count int
}
