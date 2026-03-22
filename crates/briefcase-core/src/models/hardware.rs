use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct HardwareMetadata {
    pub device_type: String, // "cuda", "metal", "cpu"
    pub device_name: String, // "NVIDIA A100", "Apple M4 Max"
    pub vram_gb: f32,
    pub compute_units: u32,
    pub provider: String, // "io.net", "akash", "lambda", "local"
    #[serde(default)]
    pub metadata: HashMap<String, serde_json::Value>,
}

impl HardwareMetadata {
    pub fn new(device_type: impl Into<String>, device_name: impl Into<String>) -> Self {
        Self {
            device_type: device_type.into(),
            device_name: device_name.into(),
            vram_gb: 0.0,
            compute_units: 0,
            provider: "unknown".into(),
            metadata: HashMap::new(),
        }
    }
}
