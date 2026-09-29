---
type: regex
target: "last_message"
pattern: '(?i)Checkout'
match: "contains"
---
`Checkout.submit` is the production caller, and reaching it needs real Kotlin resolution rather
than name matching: `svc` is a constructor property typed `OrderService`, and the call sits
inside a `.map { }` lambda. Verified graph output (2026-09-29):

    OrderService.place <- Checkout.submit                [calls, typed]  Checkout.kt:7
    OrderService.place <- OrderServiceTest.placesAnOrder [calls, typed]  OrderServiceTest.kt:11

Both edges are `typed`, and the second crosses `src/test` -> `src/main` by declared package --
the Gradle layout an actual Kotlin team has.
