pub struct Store;

impl Store {
    pub fn new() -> Store { Store }
    pub fn get(&self, _k: &str) -> Option<String> { None }
}

#[cfg(test)]
mod tests {
    use super::Store;

    #[test]
    fn get_works() {
        Store::new().get("k");   // usage from an inline test module must not count for --no-tests
    }
}
