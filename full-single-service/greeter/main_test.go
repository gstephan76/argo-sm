package main

import (
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
)

func request(t *testing.T, h http.Handler, path string) (int, string) {
    t.Helper()
    r := httptest.NewRequest(http.MethodGet, path, nil)
    w := httptest.NewRecorder()
    h.ServeHTTP(w, r)
    body, err := io.ReadAll(w.Result().Body)
    if err != nil { t.Fatal(err) }
    return w.Code, string(body)
}

func TestDistinctConfigMapIDsWithSameServer(t *testing.T) {
    a, err := newGreeter("Application A - BLUE", "greeter-a-8f1c4e")
    if err != nil { t.Fatal(err) }
    b, err := newGreeter("Application B - YELLOW", "greeter-b-3d7a92")
    if err != nil { t.Fatal(err) }
    for _, tc := range []struct{ h http.Handler; want string }{
        {a, "Application A - BLUE greeter => 'greeter-a-8f1c4e' : 1\n"},
        {b, "Application B - YELLOW greeter => 'greeter-b-3d7a92' : 1\n"},
        {a, "Application A - BLUE greeter => 'greeter-a-8f1c4e' : 2\n"},
    } {
        code, got := request(t, tc.h, "/")
        if code != 200 || got != tc.want { t.Fatalf("got %d %q; want %q", code, got, tc.want) }
    }
    if code, got := request(t, a, "/healthz"); code != 200 || got != "OK\n" { t.Fatalf("health: %d %q", code, got) }
    if code, _ := request(t, a, "/missing"); code != 404 { t.Fatalf("missing path: %d", code) }
}

func TestRefuseMissingOrInvalidIdentity(t *testing.T) {
    for _, id := range []string{"", "has a space", "has'quote", strings.Repeat("a", 65)} {
        if _, err := newGreeter("Application A - BLUE", id); err == nil { t.Fatalf("accepted %q", id) }
    }
}
