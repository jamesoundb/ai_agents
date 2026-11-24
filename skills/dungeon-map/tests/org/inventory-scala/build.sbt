ThisBuild / organization := "com.{ORG}"

lazy val inventory = (project in file("."))
  .settings(
    name := "inventory",
    libraryDependencies += "com.{ORG}.orders" % "orders-client" % "1.0.0",
    libraryDependencies += "org.typelevel" %% "cats-core" % "2.10.0"
  )
  .dependsOn(RootProject(uri("http://{HOST}/{ORG}/billing-core.git")))
