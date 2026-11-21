use std::collections::HashMap;
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use tiktoken_rs::tokenizer::{get_tokenizer, Tokenizer};
use tiktoken_rs::{get_bpe_from_tokenizer, CoreBPE};

/// Encoders built so far, one per tokenizer. Building one parses the whole
/// vocabulary (tens of milliseconds), so it is done once and shared.
static ENCODERS: LazyLock<Mutex<HashMap<Tokenizer, Arc<CoreBPE>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn encoder_for(model: &str) -> Arc<CoreBPE> {
    // Fallback to the gpt-3.5-turbo encoding if the model is not known
    let tokenizer = get_tokenizer(model).unwrap_or(Tokenizer::Cl100kBase);

    let mut encoders = ENCODERS.lock().unwrap_or_else(PoisonError::into_inner);
    Arc::clone(encoders.entry(tokenizer).or_insert_with(|| {
        // The vocabularies are compiled into tiktoken-rs, so this cannot fail.
        Arc::new(get_bpe_from_tokenizer(tokenizer).expect("bundled tokenizer vocabulary"))
    }))
}

pub fn count_tokens(text: &str, model: &str) -> usize {
    encoder_for(model).encode_with_special_tokens(text).len()
}

pub fn count_tokens_batch(texts: Vec<String>, model: &str) -> Vec<usize> {
    let encoder = encoder_for(model);
    texts
        .iter()
        .map(|t| encoder.encode_with_special_tokens(t).len())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encoder_is_built_once_per_tokenizer() {
        let first = encoder_for("gpt-4");
        let again = encoder_for("gpt-4");
        // gpt-3.5-turbo and unknown models share gpt-4's cl100k encoding.
        let sibling = encoder_for("gpt-3.5-turbo");
        let unknown = encoder_for("not-a-real-model");
        assert!(Arc::ptr_eq(&first, &again));
        assert!(Arc::ptr_eq(&first, &sibling));
        assert!(Arc::ptr_eq(&first, &unknown));

        let other = encoder_for("gpt-4o");
        assert!(!Arc::ptr_eq(&first, &other));
    }

    #[test]
    fn counts_match_a_freshly_built_encoder() {
        let text = "Hello, world! This is a token counting test.";
        for model in ["gpt-4", "gpt-4o", "gpt-3.5-turbo", "unknown-model"] {
            let fresh = tiktoken_rs::get_bpe_from_model(model)
                .or_else(|_| tiktoken_rs::get_bpe_from_model("gpt-3.5-turbo"))
                .unwrap();
            let expected = fresh.encode_with_special_tokens(text).len();
            assert!(expected > 0);
            assert_eq!(count_tokens(text, model), expected, "{model}");
            assert_eq!(
                count_tokens_batch(vec![text.to_string(), String::new()], model),
                vec![expected, 0],
                "{model}"
            );
        }
    }
}
