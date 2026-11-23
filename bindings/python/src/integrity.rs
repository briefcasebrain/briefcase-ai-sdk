//! Python bindings for `briefcase_core::integrity`: RFC 8785 canonical JSON
//! and Ed25519 helpers interchangeable with `briefcase.integrity.signing`.

use briefcase_core::integrity::signing::{self, SigningKey};
use briefcase_core::integrity::{canonical_json_v2, parse_canonical_text};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

/// Serialize a Rust value to a Python object through JSON.
pub fn json_to_py<T: serde::Serialize>(py: Python<'_>, value: &T) -> PyResult<Py<PyAny>> {
    let text = serde_json::to_string(value).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let json = py.import("json")?;
    Ok(json.call_method1("loads", (text,))?.unbind())
}

/// JSON text for a Python object: a `str` is taken as JSON text already,
/// anything else goes through `json.dumps` (NaN refused).
pub fn py_to_json_text(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = obj.cast::<PyString>() {
        return s.extract::<String>();
    }
    let json = obj.py().import("json")?;
    let kwargs = pyo3::types::PyDict::new(obj.py());
    kwargs.set_item("allow_nan", false)?;
    json.call_method("dumps", (obj,), Some(&kwargs))?.extract()
}

/// A signing key from a path (`str`) or a key file's bytes.
pub fn load_signing_key(key: &Bound<'_, PyAny>) -> PyResult<SigningKey> {
    if let Ok(bytes) = key.cast::<PyBytes>() {
        return SigningKey::from_key_bytes(bytes.as_bytes())
            .map_err(|e| PyValueError::new_err(e.to_string()));
    }
    let path: String = key.str()?.extract()?;
    SigningKey::from_file(&path).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// RFC 8785 canonical text of JSON text (`jcs-1`). With `stored=True` the
/// input is read the way verifiers read stored canonical text (numbers
/// beyond 2**53 as doubles).
#[pyfunction]
#[pyo3(signature = (json_text, stored=false))]
pub fn jcs_canonicalize(json_text: &str, stored: bool) -> PyResult<String> {
    let value = if stored {
        parse_canonical_text(json_text)
    } else {
        serde_json::from_str(json_text)
    }
    .map_err(|e| PyValueError::new_err(e.to_string()))?;
    canonical_json_v2(&value).map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Sign the raw bytes of a hex digest with a key (path or key-file bytes).
#[pyfunction]
pub fn ed25519_sign_digest(key: &Bound<'_, PyAny>, digest_hex: &str) -> PyResult<String> {
    load_signing_key(key)?
        .sign_digest(digest_hex)
        .map_err(|e| PyValueError::new_err(e.to_string()))
}

/// Verify a digest signature against a JWK `x`; never raises on bad input.
#[pyfunction]
pub fn ed25519_verify_digest(digest_hex: &str, signature: &str, public_x: &str) -> bool {
    signing::verify_digest(digest_hex, signature, public_x)
}

/// Public JWK of a key (path or key-file bytes).
#[pyfunction]
pub fn ed25519_public_jwk(py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    json_to_py(py, &load_signing_key(key)?.public_jwk())
}

/// RFC 7638 thumbprint of an Ed25519 public key given by its JWK `x`.
#[pyfunction]
pub fn ed25519_key_id(public_x: &str) -> String {
    signing::jwk_thumbprint(public_x)
}
