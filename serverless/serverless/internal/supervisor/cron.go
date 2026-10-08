package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type scheduleRecord struct {
	Name, Cron, Timezone string
	Spec                 Spec
}
type schedule struct {
	record    scheduleRecord
	running   bool
	lastError string
	fields    [5]map[int]bool
	spec      Spec
	zone      *time.Location
	last      int64
}

func cronFields(expr string) ([5]map[int]bool, error) {
	var fields [5]map[int]bool
	parts := strings.Fields(expr)
	if len(parts) != 5 {
		return fields, fmt.Errorf("cron requires five fields")
	}
	lo := []int{0, 0, 1, 1, 0}
	hi := []int{59, 23, 31, 12, 6}
	for i, part := range parts {
		fields[i] = map[int]bool{}
		for _, item := range strings.Split(part, ",") {
			step := 1
			bits := strings.Split(item, "/")
			if len(bits) > 2 {
				return fields, fmt.Errorf("invalid cron step")
			}
			if len(bits) == 2 {
				var err error
				step, err = strconv.Atoi(bits[1])
				if err != nil || step < 1 {
					return fields, fmt.Errorf("invalid cron step")
				}
			}
			start, end := lo[i], hi[i]
			if bits[0] != "*" {
				r := strings.Split(bits[0], "-")
				var err error
				start, err = strconv.Atoi(r[0])
				if err != nil {
					return fields, err
				}
				end = start
				if len(r) == 2 {
					end, err = strconv.Atoi(r[1])
					if err != nil {
						return fields, err
					}
				} else if len(r) > 2 {
					return fields, fmt.Errorf("invalid cron range")
				}
			}
			if start < lo[i] || end > hi[i] || end < start {
				return fields, fmt.Errorf("cron value out of range")
			}
			for n := start; n <= end; n += step {
				fields[i][n] = true
			}
		}
	}
	return fields, nil
}
func (sc *schedule) tick(s *Service, now time.Time) {
	n := now.In(sc.zone)
	minute := now.Unix() / 60
	if sc.last == minute {
		return
	}
	sc.last = minute
	if !sc.matches(n) || sc.running {
		return
	}
	sc.running = true
	go func() {
		ctx, cancel := context.WithTimeout(s.ctx, time.Duration(sc.spec.Timeout)*time.Second)
		defer cancel()
		_, err := s.Engine.Invoke(ctx, sc.spec, 1, 0)
		s.mu.Lock()
		sc.running = false
		if err != nil {
			sc.lastError = err.Error()
		} else {
			sc.lastError = ""
		}
		s.mu.Unlock()
	}()
}
func (s *Service) scheduleHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == "GET" {
		s.mu.Lock()
		defer s.mu.Unlock()
		records := map[string]any{}
		for name, sc := range s.schedules {
			records[name] = map[string]any{"cron": sc.record.Cron, "timezone": sc.record.Timezone, "running": sc.running, "last_error": sc.lastError}
		}
		reply(w, 200, records)
		return
	}
	if r.Method == "DELETE" {
		s.mu.Lock()
		defer s.mu.Unlock()
		name := strings.TrimPrefix(r.URL.Path, "/v1/schedules/")
		old := s.schedules[name]
		delete(s.schedules, name)
		if err := s.saveSchedules(); err != nil {
			if old != nil {
				s.schedules[name] = old
			}
			failure(w, err)
			return
		}
		reply(w, 200, map[string]string{"deleted": name})
		return
	}
	if r.Method != "POST" {
		reply(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var v scheduleRecord
	if err := decode(w, r, &v); err != nil {
		reply(w, 400, map[string]string{"error": err.Error()})
		return
	}
	if err := v.Spec.Validate(); err != nil {
		reply(w, 400, map[string]string{"error": err.Error()})
		return
	}
	fields, err := cronFields(v.Cron)
	if err != nil {
		reply(w, 400, map[string]string{"error": err.Error()})
		return
	}
	if v.Timezone == "" {
		v.Timezone = "UTC"
	}
	zone, err := time.LoadLocation(v.Timezone)
	if err != nil || v.Name == "" {
		reply(w, 400, map[string]string{"error": "invalid timezone or name"})
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.schedules) >= 1024 {
		failure(w, &Rejection{Reason: "schedule limit reached"})
		return
	}
	old := s.schedules[v.Name]
	s.schedules[v.Name] = &schedule{record: v, fields: fields, spec: v.Spec, zone: zone, last: time.Now().Unix() / 60}
	if err := s.saveSchedules(); err != nil {
		delete(s.schedules, v.Name)
		if old != nil {
			s.schedules[v.Name] = old
		}
		failure(w, err)
		return
	}
	reply(w, 201, map[string]string{"name": v.Name, "cron": v.Cron})
}

func (sc *schedule) matches(n time.Time) bool {
	if !sc.fields[0][n.Minute()] || !sc.fields[1][n.Hour()] || !sc.fields[3][int(n.Month())] {
		return false
	}
	parts := strings.Fields(sc.record.Cron)
	day, weekday := sc.fields[2][n.Day()], sc.fields[4][int(n.Weekday())]
	if len(parts) == 5 && parts[2] != "*" && parts[4] != "*" {
		return day || weekday
	}
	return day && weekday
}
func (s *Service) SetScheduleState(path string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.scheduleState = path
	if path == "" {
		return nil
	}
	b, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var records []scheduleRecord
	if err = json.Unmarshal(b, &records); err != nil {
		return err
	}
	if len(records) > 1024 {
		return fmt.Errorf("schedule state exceeds limit")
	}
	for _, v := range records {
		f, err := cronFields(v.Cron)
		if err != nil {
			return err
		}
		z, err := time.LoadLocation(v.Timezone)
		if err != nil {
			return err
		}
		if err = v.Spec.Validate(); err != nil {
			return err
		}
		s.schedules[v.Name] = &schedule{record: v, fields: f, spec: v.Spec, zone: z, last: time.Now().Unix() / 60}
	}
	return nil
}
func (s *Service) saveSchedules() error {
	if s.scheduleState == "" {
		return nil
	}
	var records []scheduleRecord
	for _, sc := range s.schedules {
		records = append(records, sc.record)
	}
	b, err := json.Marshal(records)
	if err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(s.scheduleState), 0700); err != nil {
		return err
	}
	tmp := s.scheduleState + ".tmp"
	if err = os.WriteFile(tmp, b, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, s.scheduleState)
}
