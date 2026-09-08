output "{{service_underscore}}_id" {
  description = "Id of the {{service}} primary resource."
  value       = module.{{service_underscore}}.id
}
