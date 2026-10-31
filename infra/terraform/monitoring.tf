# Alerts on the three things the brief cares about: spend cap, provider outages, and
# messages handed to humans. They key off log lines the app already writes.

resource "google_monitoring_notification_channel" "email" {
  display_name = "Riverbend on-call"
  type         = "email"
  labels       = { email_address = var.alert_email }
}

locals {
  log_metrics = {
    budget_exhausted = {
      filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"reached its monthly LLM cap\""
      text   = "A shop hit its monthly LLM cap. New messages are held (not lost) until the cap is raised or the month rolls over."
    }
    escalated_ai_unavailable = {
      filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"escalated:ai_unavailable\""
      text   = "Messages are being handed to staff because the model provider is unavailable."
    }
    invalid_model_output = {
      filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"escalated:invalid_model_output\""
      text   = "The model produced output that failed validation twice. Check for prompt/schema drift."
    }
    permanent_provider_error = {
      filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"permanent provider error\""
      text   = "Provider rejected requests as invalid/unauthorised: likely a configuration or quota problem."
    }
  }
}

resource "google_logging_metric" "m" {
  for_each = local.log_metrics
  name     = "riverbend/${each.key}"
  filter   = each.value.filter
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "a" {
  for_each     = local.log_metrics
  display_name = "Riverbend: ${replace(each.key, "_", " ")}"
  combiner     = "OR"

  conditions {
    display_name = each.key
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.m[each.key].name}\" AND resource.type=\"cloud_run_revision\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  documentation {
    content = each.value.text
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
}
