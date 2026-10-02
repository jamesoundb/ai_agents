from .vault import Vault


class LazyHolder:
    """Fields typed through `x or Ctor()` and a property that wraps a private field (Django's
    QuerySet: `self._query = query or sql.Query(model)` + `@property def query`)."""

    def __init__(self, vault=None, flag=False):
        self._vault = vault or Vault()             # typed: first operand naming a type
        self.alt = Vault() if flag else vault      # typed: conditional expression

    @property
    def vault(self):
        return self._vault

    @property
    def other(self) -> Vault:
        return make()

    def run(self, item):
        self.vault.put(item)                       # typed through the property returning `_vault`
        self._vault.put(item)                      # typed through `vault or Vault()`
        self.alt.put(item)                         # typed through the conditional
        self.other.put(item)                       # typed through the property's return annotation
        local = self._vault or Vault()
        local.put(item)                            # local bound from an `or`


def make():
    return Vault()
