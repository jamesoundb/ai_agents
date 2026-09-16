import { Repo, load } from './repo.js';
import * as util from './util.js';
const fs = require('fs');
import './theme.css';                 // asset: neither an edge nor an unresolved import
import logo from './logo.svg?url';    // asset with a query
import { fmt2 } from '@fx/util';      // workspace package -> packages/util/src/index.ts (dist mapped to src)

export class Service {
  run() {
    const repo = new Repo();
    repo.save(1);        // typed: local declared via new Repo()
    load();              // free name from an import -> import
    util.fmt(2);         // namespace alias of a repo module -> import
    fs.readFile('x');    // external require -> unresolved, never util.readFile
    fmt2(3);             // workspace package function -> import
  }
}

export function loose(x) {
  x.save(2);             // unknown receiver -> ambiguous
}
