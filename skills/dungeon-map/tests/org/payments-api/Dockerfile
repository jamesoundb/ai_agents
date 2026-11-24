ARG BASE={HOST}/{ORG}/base-image:1.2
FROM golang:1.22 AS build
WORKDIR /src
FROM ${BASE}
COPY --from=build /out/app /app
