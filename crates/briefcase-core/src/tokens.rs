use tiktoken_rs::get_bpe_from_model;

pub fn count_tokens(text: &str, model: &str) -> usize {
    let bpe = match get_bpe_from_model(model) {
        Ok(bpe) => bpe,
        Err(_) => {
            // Fallback to gpt-3.5-turbo encoding if model not found
            get_bpe_from_model("gpt-3.5-turbo").unwrap()
        }
    };
    bpe.encode_with_special_tokens(text).len()
}

pub fn count_tokens_batch(texts: Vec<String>, model: &str) -> Vec<usize> {
    texts.into_iter().map(|t| count_tokens(&t, model)).collect()
}
