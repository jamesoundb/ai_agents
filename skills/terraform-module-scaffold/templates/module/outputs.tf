output "id" {
  description = "Fully qualified id of the primary resource."
  value       = google_pubsub_topic.main.id
}

output "name" {
  description = "Name of the primary resource."
  value       = google_pubsub_topic.main.name
}
