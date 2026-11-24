pluginManagement {
  includeBuild("../gradle-conventions")
}

rootProject.name = "orders-service"
include(":app", ":client")
