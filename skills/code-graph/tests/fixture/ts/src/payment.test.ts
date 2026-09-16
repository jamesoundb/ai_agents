import { Orchestrator, StripeGateway } from './payment';
new Orchestrator(new StripeGateway()).process();
