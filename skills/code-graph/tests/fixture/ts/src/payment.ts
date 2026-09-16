export interface Gateway {
  charge(amount: number): boolean;
}

export class StripeGateway implements Gateway {
  charge(amount: number): boolean { return amount > 0; }
}

export class Orchestrator {
  private gw: StripeGateway;
  constructor(gw: StripeGateway) { this.gw = gw; }
  process(): boolean {
    return this.gw.charge(1);   // typed via field type -> StripeGateway.charge
  }
}
