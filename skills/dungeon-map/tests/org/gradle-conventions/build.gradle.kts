plugins {
  `kotlin-dsl`
}

group = "com.{ORG}.gradle"

gradlePlugin {
  plugins {
    create("conventions") {
      id = "com.{ORG}.conventions"
      implementationClass = "com.{ORG}.gradle.ConventionsPlugin"
    }
  }
}
