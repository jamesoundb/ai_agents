package store

// Base provides Ping; Derived embeds Base, so d.Ping() must resolve to Base.Ping (typed).
type Base struct{}

func (b *Base) Ping() string { return "pong" }

type Derived struct {
	Base
}

func UseDerived() string {
	d := &Derived{}
	return d.Ping()
}
