use briefcase_core::CostCalculator;
fn main() {
    let c = CostCalculator::new();
    let mut m: Vec<_> = c.get_all_models().into_iter().cloned().collect();
    m.sort_by(|a, b| a.model_name.cmp(&b.model_name));
    println!("{}", serde_json::to_string(&serde_json::json!({"models": m})).unwrap());
}
