plugins {
  alias(libs.plugins.org.conventions)
}

dependencies {
  implementation(libs.billing.impl)
  implementation(platform("com.{ORG}.platform:platform-bom:1.4.0"))
  implementation(project(":client"))
  implementation(libs.guava)
}
