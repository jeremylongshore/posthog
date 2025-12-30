use chrono::{DateTime, Duration, SecondsFormat, Utc};
use common_redis::Client;
use std::collections::HashMap;
use tracing::{info, warn};
use uuid::Uuid;

const ISSUE_BUCKET_TTL_SECONDS: usize = 60 * 60;
const ISSUE_BUCKET_INTERVAL_MINUTES: i64 = 5;
const SPIKE_MULTIPLIER: f64 = 10.0;
const NUM_BUCKETS: usize = 12;

fn round_datetime_to_minutes(datetime: DateTime<Utc>, minutes: i64) -> DateTime<Utc> {
    assert!(minutes > 0, "minutes must be > 0");
    let bucket_seconds = minutes * 60;
    let now_ts = datetime.timestamp();
    let rounded_ts = now_ts - now_ts.rem_euclid(bucket_seconds);
    DateTime::<Utc>::from_timestamp(rounded_ts, 0).expect("rounded timestamp is always valid")
}

fn get_rounded_to_minutes(datetime: DateTime<Utc>, minutes: i64) -> String {
    round_datetime_to_minutes(datetime, minutes).to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn get_now_rounded_to_minutes(minutes: i64) -> String {
    get_rounded_to_minutes(Utc::now(), minutes)
}

async fn try_increment_issue_buckets(
    redis: &(dyn Client + Send + Sync),
    issue_counts: &HashMap<Uuid, u32>,
) {
    if issue_counts.is_empty() {
        return;
    }

    let now_rounded_to_minutes = get_now_rounded_to_minutes(ISSUE_BUCKET_INTERVAL_MINUTES);
    let items: Vec<(String, i64)> = issue_counts
        .iter()
        .map(|(issue_id, count)| {
            (
                format!("issue-buckets:{issue_id}-{now_rounded_to_minutes}"),
                *count as i64,
            )
        })
        .collect();

    if let Err(err) = redis
        .batch_incr_by_expire_nx(items, ISSUE_BUCKET_TTL_SECONDS)
        .await
    {
        warn!("Failed to increment issue buckets batch: {err}");
    }
}

pub async fn do_spike_detection(
    redis: &(dyn Client + Send + Sync),
    issue_counts: HashMap<Uuid, u32>,
) {
    if issue_counts.is_empty() {
        return;
    }

    try_increment_issue_buckets(redis, &issue_counts).await;

    let issue_ids: Vec<Uuid> = issue_counts.keys().copied().collect();
    match get_spiking_issues(redis, issue_ids).await {
        Ok(spiking) => {
            for spike in spiking {
                info!(
                    issue_id = %spike.issue_id,
                    baseline = spike.computed_baseline,
                    current = spike.current_bucket_value,
                    "Spike detected"
                );
            }
        }
        Err(err) => {
            warn!("Failed to detect spikes: {err}");
        }
    }
}

#[derive(Debug, Clone)]
pub struct SpikingIssue {
    pub issue_id: Uuid,
    pub computed_baseline: f64,
    pub current_bucket_value: i64,
}

async fn get_spiking_issues(
    redis: &(dyn Client + Send + Sync),
    issue_ids: Vec<Uuid>,
) -> Result<Vec<SpikingIssue>, common_redis::CustomRedisError> {
    if issue_ids.is_empty() {
        return Ok(vec![]);
    }

    let now = Utc::now();
    let bucket_timestamps: Vec<String> = (0..NUM_BUCKETS)
        .map(|i| {
            let offset = Duration::minutes(ISSUE_BUCKET_INTERVAL_MINUTES * i as i64);
            get_rounded_to_minutes(now - offset, ISSUE_BUCKET_INTERVAL_MINUTES)
        })
        .collect();

    let keys: Vec<String> = issue_ids
        .iter()
        .flat_map(|issue_id| {
            bucket_timestamps
                .iter()
                .map(move |ts| format!("issue-buckets:{issue_id}-{ts}"))
        })
        .collect();

    let values = redis.mget(keys).await?;

    let mut spiking = Vec::new();

    for (issue_idx, issue_id) in issue_ids.iter().enumerate() {
        let start_idx = issue_idx * NUM_BUCKETS;
        let issue_values = &values[start_idx..start_idx + NUM_BUCKETS];

        let sum: i64 = issue_values.iter().filter_map(|v| *v).sum();
        let computed_baseline = sum as f64 / NUM_BUCKETS as f64;
        let current_bucket_value = issue_values[0].unwrap_or(0);

        let is_spiking = if computed_baseline == 0.0 {
            current_bucket_value > 0
        } else {
            current_bucket_value as f64 > computed_baseline * SPIKE_MULTIPLIER
        };

        if is_spiking {
            spiking.push(SpikingIssue {
                issue_id: *issue_id,
                computed_baseline,
                current_bucket_value,
            });
        }
    }

    Ok(spiking)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use common_redis::MockRedisClient;

    #[test]
    fn test_get_rounded_to_minutes_floor_rounding() {
        let dt = Utc.with_ymd_and_hms(2025, 12, 16, 12, 34, 56).unwrap();
        assert_eq!(
            get_rounded_to_minutes(dt, 5),
            "2025-12-16T12:30:00Z".to_string()
        );
    }

    #[test]
    fn test_get_rounded_to_minutes_exact_boundary() {
        let dt = Utc.with_ymd_and_hms(2025, 12, 16, 12, 35, 0).unwrap();
        assert_eq!(
            get_rounded_to_minutes(dt, 5),
            "2025-12-16T12:35:00Z".to_string()
        );
    }

    #[test]
    fn test_get_rounded_to_minutes_12() {
        let dt = Utc.with_ymd_and_hms(2025, 12, 16, 12, 34, 56).unwrap();
        assert_eq!(
            get_rounded_to_minutes(dt, 12),
            "2025-12-16T12:24:00Z".to_string()
        );
    }

    #[tokio::test]
    async fn test_get_spiking_issues_empty() {
        let redis = MockRedisClient::new();
        let result = get_spiking_issues(&redis, vec![]).await.unwrap();
        assert!(result.is_empty());
    }

    #[tokio::test]
    async fn test_get_spiking_issues_detects_spike() {
        let mut redis = MockRedisClient::new();
        let issue_id = Uuid::new_v4();

        let now = Utc::now();
        for i in 0..NUM_BUCKETS {
            let offset = Duration::minutes(ISSUE_BUCKET_INTERVAL_MINUTES * i as i64);
            let ts = get_rounded_to_minutes(now - offset, ISSUE_BUCKET_INTERVAL_MINUTES);
            let key = format!("issue-buckets:{issue_id}-{ts}");

            let value = if i == 0 { Some(100) } else { Some(1) };
            redis.mget_ret(&key, value);
        }

        let result = get_spiking_issues(&redis, vec![issue_id]).await.unwrap();

        assert_eq!(result.len(), 1);
        assert_eq!(result[0].issue_id, issue_id);
        assert!((result[0].computed_baseline - (100.0 + 11.0) / 12.0).abs() < 0.01);
        assert_eq!(result[0].current_bucket_value, 100);
    }

    #[tokio::test]
    async fn test_get_spiking_issues_no_spike() {
        let mut redis = MockRedisClient::new();
        let issue_id = Uuid::new_v4();

        let now = Utc::now();
        for i in 0..NUM_BUCKETS {
            let offset = Duration::minutes(ISSUE_BUCKET_INTERVAL_MINUTES * i as i64);
            let ts = get_rounded_to_minutes(now - offset, ISSUE_BUCKET_INTERVAL_MINUTES);
            let key = format!("issue-buckets:{issue_id}-{ts}");
            redis.mget_ret(&key, Some(10));
        }

        let result = get_spiking_issues(&redis, vec![issue_id]).await.unwrap();
        assert!(result.is_empty());
    }

    #[tokio::test]
    async fn test_get_spiking_issues_zero_baseline() {
        let mut redis = MockRedisClient::new();
        let issue_id = Uuid::new_v4();

        let now = Utc::now();
        for i in 0..NUM_BUCKETS {
            let offset = Duration::minutes(ISSUE_BUCKET_INTERVAL_MINUTES * i as i64);
            let ts = get_rounded_to_minutes(now - offset, ISSUE_BUCKET_INTERVAL_MINUTES);
            let key = format!("issue-buckets:{issue_id}-{ts}");
            let value = if i == 0 { Some(1) } else { None };
            redis.mget_ret(&key, value);
        }

        let result = get_spiking_issues(&redis, vec![issue_id]).await.unwrap();
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].issue_id, issue_id);
        assert_eq!(result[0].current_bucket_value, 1);
    }
}

