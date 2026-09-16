package store

// MemStore is an in-memory key/value store.
type MemStore struct {
	items map[string]string
}

// New returns an empty MemStore.
func New() *MemStore {
	return &MemStore{items: map[string]string{}}
}

// Get returns the value for k.
func (s *MemStore) Get(k string) string {
	return s.items[k]
}
