import { Repo, fmt, loadAll } from './index.js';   // everything comes through the barrel

export function useBarrel() {
  const r = new Repo();
  r.save(1);        // typed through barrel re-export -> repo.js Repo.save
  fmt(2);           // wildcard re-export -> util.js fmt
  loadAll();        // aliased re-export -> repo.js load
}
