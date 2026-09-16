pub trait Sinker {
    fn emit(&self) -> u32;
}

pub struct Holder<S: Sinker> {
    pub sink: S,
}

impl<S: Sinker> Holder<S> {
    pub fn go(&self) -> u32 {
        self.sink.emit()          // field typed by a generic parameter: resolve through its bound -> Sinker.emit
    }
}

pub fn emit() -> u32 {            // same name in the same file: must NOT be picked for self.sink.emit()
    0
}

pub fn build() -> Result<crate::store::Store, String> {
    Ok(crate::store::Store::new())
}

pub fn use_built() -> Option<String> {
    let s = build().unwrap();     // unwrap -> Store
    let t = build()?;             // ? -> Store   (function returns Option/Result-compatible for the fixture)
    s.get("k");
    t.get("k")
}
