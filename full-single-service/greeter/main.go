package main

import (
    "fmt"
    "log"
    "net/http"
    "os"
    "regexp"
    "strings"
    "sync/atomic"
    "time"
)

var validID = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,64}$`)

type greeter struct {
    prefix string
    instanceID string
    requests atomic.Uint64
}

func newGreeter(prefix, instanceID string) (http.Handler, error) {
    if !validID.MatchString(instanceID) {
        return nil, fmt.Errorf("GREETER_INSTANCE_ID must be 1..64 letters, digits, dots, dashes or underscores")
    }
    if !regexp.MustCompile(`^Application [AB] - (BLUE|YELLOW)$`).MatchString(prefix) {
        return nil, fmt.Errorf("MESSAGE_PREFIX must be Application A/B - BLUE/YELLOW")
    }
    g := &greeter{prefix: prefix, instanceID: instanceID}
    mux := http.NewServeMux()
    mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/" { http.NotFound(w, r); return }
        if r.Method != http.MethodGet { w.WriteHeader(http.StatusMethodNotAllowed); return }
        w.Header().Set("Content-Type", "text/plain; charset=utf-8")
        fmt.Fprintf(w, "%s greeter => '%s' : %d\n", g.prefix, g.instanceID, g.requests.Add(1))
    })
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
        if r.Method != http.MethodGet { w.WriteHeader(http.StatusMethodNotAllowed); return }
        w.Header().Set("Content-Type", "text/plain; charset=utf-8")
        fmt.Fprintln(w, "OK")
    })
    return mux, nil
}

func main() {
    prefix := strings.TrimSpace(os.Getenv("MESSAGE_PREFIX"))
    id := strings.TrimSpace(os.Getenv("GREETER_INSTANCE_ID"))
    handler, err := newGreeter(prefix, id)
    if err != nil { log.Fatal(err) }
    server := &http.Server{
        Addr: ":8080",
        Handler: handler,
        ReadHeaderTimeout: 5 * time.Second,
        IdleTimeout: 60 * time.Second,
    }
    log.Printf("Greeter listening on :8080 (prefix=%s, ID=%s)", prefix, id)
    log.Fatal(server.ListenAndServe())
}
