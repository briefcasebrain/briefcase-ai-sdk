use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataRef {
    pub uri: String,
    pub fingerprint: String,
    pub version: Option<String>,
    pub metadata: Option<serde_json::Value>,
}

impl DataRef {
    pub fn new(uri: String, fingerprint: String) -> Self {
        Self {
            uri,
            fingerprint,
            version: None,
            metadata: None,
        }
    }
}
