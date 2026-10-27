package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPropagateTraceHeaders(t *testing.T) {
	src := http.Header{}
	src.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
	src.Set("Tracestate", "vendor=value")
	src.Set("X-Request-Id", "req-123")
	src.Set("X-B3-Traceid", "aaaabbbbccccdddd")
	src.Set("B3", "aaaabbbbccccdddd-1234123412341234-1")
	src.Add("Baggage", "demo=a")
	src.Set("Authorization", "Bearer should-not-forward")
	dst := http.Header{}
	propagateTraceHeaders(dst, src)
	for _, name := range []string{"Traceparent", "Tracestate", "X-Request-Id", "X-B3-Traceid", "B3", "Baggage"} {
		if dst.Get(name) != src.Get(name) {
			t.Errorf("%s not forwarded: %q", name, dst.Get(name))
		}
	}
	if dst.Get("Authorization") != "" {
		t.Fatal("credentials must not be copied")
	}
}

func TestTracks(t *testing.T) {
	h := http.Header{}
	if track(h, "x-db-track") != "stable" {
		t.Fatal("default")
	}
	h.Set("x-db-track", "canary")
	if track(h, "x-db-track") != "canary" {
		t.Fatal("canary")
	}
	h.Set("x-db-track", "other")
	if track(h, "x-db-track") != "stable" {
		t.Fatal("unexpected")
	}
}
func TestInfo(t *testing.T) {
	s := &server{role: "front", app: "A", prefix: "Application A - BLUE"}
	r := httptest.NewRequest("GET", "/info", nil)
	w := httptest.NewRecorder()
	s.routes().ServeHTTP(w, r)
	if !strings.Contains(w.Body.String(), "Application A - BLUE") {
		t.Fatal(w.Body.String())
	}
}
