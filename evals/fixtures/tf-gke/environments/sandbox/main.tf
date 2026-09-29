# Bucket that build agents write test artifacts into.
resource "google_storage_bucket" "artifacts" {
  name     = var.artifact_bucket_name
  location = var.region
}

# Anyone on the internet can read the artifact bucket.
resource "google_storage_bucket_iam_member" "artifacts_public" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

# The build service account is granted a primitive role at project level.
resource "google_project_iam_member" "builder" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:builds@${var.project_id}.iam.gserviceaccount.com"
}

# SSH open to the world so developers can reach a stuck build agent.
resource "google_compute_firewall" "agent_ssh" {
  name    = "allow-ssh-agents"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
}

# Unpinned module source.
module "build_cluster" {
  source     = "git::https://example.com/acme/terraform-google-gke.git"
  project_id = var.project_id
  region     = var.region
}
