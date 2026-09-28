/// HTTP client for widget upload/status endpoints.

use std::time::Duration;

use reqwest::multipart;

#[derive(Debug)]
pub struct WidgetApi {
    base_url: String,
    token: String,
    client: reqwest::Client,
}

#[derive(Debug, serde::Deserialize)]
pub struct UploadResponse {
    pub reference_id: String,
    pub status: String,
}

#[derive(Debug, serde::Deserialize)]
pub struct StatusResponse {
    pub reference_id: String,
    pub status: Option<String>,
    pub content: Option<String>,
}

impl WidgetApi {
    pub fn new(base_url: &str, token: &str) -> Self {
        Self {
            base_url: base_url.trim_end_matches('/').to_string(),
            token: token.to_string(),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(1800))
                .build()
                .expect("Failed to build HTTP client"),
        }
    }

    pub async fn upload_audio(&self, audio_bytes: Vec<u8>, filename: &str) -> Result<UploadResponse, String> {
        let part = multipart::Part::bytes(audio_bytes)
            .file_name(filename.to_string())
            .mime_str("audio/m4a")
            .map_err(|e| e.to_string())?;

        let form = multipart::Form::new().part("file", part);

        let resp = self
            .client
            .post(format!("{}/api/widget/upload", self.base_url))
            .bearer_auth(&self.token)
            .multipart(form)
            .send()
            .await
            .map_err(|e| format!("Upload failed: {e}"))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(format!("Upload error {status}: {body}"));
        }

        resp.json::<UploadResponse>()
            .await
            .map_err(|e| format!("Parse error: {e}"))
    }

    pub async fn poll_status(&self, reference_id: &str) -> Result<StatusResponse, String> {
        let resp = self
            .client
            .get(format!(
                "{}/api/widget/status/{reference_id}",
                self.base_url
            ))
            .bearer_auth(&self.token)
            .send()
            .await
            .map_err(|e| format!("Poll failed: {e}"))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(format!("Poll error {status}: {body}"));
        }

        resp.json::<StatusResponse>()
            .await
            .map_err(|e| format!("Parse error: {e}"))
    }
}
