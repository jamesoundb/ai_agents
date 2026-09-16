import { Orchestrator, StripeGateway } from '@pay/payment';   // tsconfig paths alias -> ts/src/payment.ts

export function checkout(): boolean {
  return new Orchestrator(null as any).process();
}

function gateway(): StripeGateway { return new StripeGateway(); }

export function pay(): boolean {
  const g = gateway();
  const a = g.charge(2);          // local typed from gateway()'s return type
  return a && gateway().charge(3); // call-segment receiver
}
