mod store;
mod sinks;
use std::collections::HashMap;
use store::Store;
use crate::store::Store as StoreAgain;   // crate:: path must resolve to src/store.rs
use fixture_util::helper;                // workspace crate (name with dash -> underscore)

pub fn run() {
    let s = Store::new();                       // typed via uppercase container
    s.get("k");                                 // typed: local s declared as Store
    let m: HashMap<String, String> = HashMap::new();
    m.get("x");                                 // external declared type -> unresolved, never Store.get
    helper();                                   // import from the workspace crate
}
