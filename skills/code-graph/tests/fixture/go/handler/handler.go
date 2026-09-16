package handler

import (
	"example.com/fixture/go/mock"
	"example.com/fixture/go/store"

	api "google.golang.org/api/container/v1"
	"github.com/external/sdk/helper/schema"
)

// Handler wires the store to an external API client.
type Handler struct {
	S      *store.MemStore
	Client *api.Service
}

// Serve exercises every receiver class the linker must tell apart.
func (h *Handler) Serve(d *schema.ResourceData) {
	h.S.Get("k")                          // typed: field S -> store.MemStore.Get
	d.Get("x")                            // external: *schema.ResourceData is not ours -> unresolved
	h.Client.Projects.Locations.Get("p")  // external: chain leaves the repo through an external field type
	store.New()                           // namespace: imported repo package -> import confidence
	m := &mock.ResourceDataMock{}         // instantiates, import confidence (legit)
	m.Get("y")                            // typed: local m -> mock.ResourceDataMock.Get
	c := undefinedClient()
	c.Get("z")                            // callee not in the graph: unknown receiver -> at most a lead (ambiguous)
	g := getStore()
	g.Get("k")                            // local typed from getStore's return type -> typed MemStore.Get
	_ = schema.Resource{}                 // external type: no instantiates edge
}

func getStore() *store.MemStore { return store.New() }
