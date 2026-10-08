package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"mini-functions/internal/supervisor"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	stateFile := flag.String("schedule-state", "", "durable cron configuration JSON file")
	echo := flag.Bool("benchmark-echo", false, "synthetic control-plane baseline; executes no guests")
	role := flag.String("role", "standalone", "standalone, worker, or router")
	root := flag.String("mini-os-root", ".mini-runtime-v2", "built v2 runtime")
	addr := flag.String("listen", ":8080", "HTTP address")
	accel := flag.String("accel", "auto", "auto, tcg, kvm")
	cpus := flag.Float64("cpu-budget", 3, "aggregate supervisor CPU cores")
	memory := flag.Int("memory-budget", 6144, "aggregate supervisor memory MiB")
	poolMin := flag.Int("pool-min", 8, "minimum unused paused guests")
	poolMax := flag.Int("pool-max", 64, "maximum unused paused guests")
	childRouter := flag.String("child-router", os.Getenv("FUNCTION_ROUTER_URL"), "distributed fanout router URL")
	calibrate := flag.Int("calibrate", 100, "cold lifecycle calibration runs")
	workers := flag.String("workers", "", "comma-separated worker URLs for router")
	flag.Parse()
	token := os.Getenv("FUNCTION_TOKEN")
	if len(token) < 16 {
		log.Fatal("FUNCTION_TOKEN must contain at least 16 characters")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var handler http.Handler
	var cleanup func()
	if *echo {
		service := supervisor.NewBenchmarkService(ctx, token)
		handler = service
		cleanup = service.Close
	} else if *role == "router" {
		router, err := supervisor.NewRouter(ctx, *workers, token)
		if err != nil {
			log.Fatal(err)
		}
		handler = router
		cleanup = router.Close
	} else {
		if *role != "standalone" && *role != "worker" {
			log.Fatal("invalid role")
		}
		var dispatcher func(context.Context, supervisor.Spec, int) (*supervisor.Result, error)
		if *role == "worker" && *childRouter != "" {
			dispatcher = supervisor.RemoteDispatcher(*childRouter, token)
		}
		engine, err := supervisor.NewEngine(ctx, supervisor.Config{Dispatcher: dispatcher, Root: *root, Accel: *accel, CPUs: *cpus, MemoryMB: *memory, PoolMin: *poolMin, PoolMax: *poolMax, Calibrate: *calibrate, Name: os.Getenv("POD_NAME"), Node: os.Getenv("NODE_NAME")})
		if err != nil {
			log.Fatal(err)
		}
		service := supervisor.NewService(engine, token)
		if err := service.SetScheduleState(*stateFile); err != nil {
			service.Close()
			engine.Close()
			log.Fatal(err)
		}
		handler = service
		cleanup = func() { service.Close(); engine.Close() }
		fmt.Printf("ready: %+v\n", engine.Metrics())
	}
	server := &http.Server{Addr: *addr, Handler: handler, ReadTimeout: 5 * time.Second, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		server.Shutdown(c)
		cleanup()
	}()
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		stop()
		<-shutdownDone
		log.Fatal(err)
	}
	<-shutdownDone
}
