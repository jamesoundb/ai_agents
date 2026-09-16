package mock

// ResourceDataMock mimics the SDK's ResourceData in tests. It must NOT become a hub
// just because unrelated code calls a method named Get on some other type.
type ResourceDataMock struct{}

// Get returns nil.
func (m *ResourceDataMock) Get(k string) interface{} {
	return nil
}
