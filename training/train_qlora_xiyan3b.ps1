param(
  [string]$ModelPath = "Z:\python\Projects\task\datasources\XiYanSQL-QwenCoder-3b\XiYanSQL-QwenCoder-3B-2504",
  [string]$Dataset = "training\sft_train.jsonl",
  [string]$OutputDir = "outputs\xiyan3b-qlora",
  [int]$Epochs = 2,
  [int]$MaxLength = 4096
)

if (-not (Get-Command swift -ErrorAction SilentlyContinue)) {
  Write-Error "未找到 ms-swift 的 swift 命令。请先在训练环境安装：pip install ms-swift[llm]"
  exit 1
}

swift sft `
  --model $ModelPath `
  --dataset $Dataset `
  --train_type lora `
  --torch_dtype bfloat16 `
  --num_train_epochs $Epochs `
  --max_length $MaxLength `
  --learning_rate 1e-4 `
  --lora_rank 16 `
  --lora_alpha 32 `
  --target_modules all-linear `
  --gradient_accumulation_steps 8 `
  --per_device_train_batch_size 1 `
  --save_steps 100 `
  --output_dir $OutputDir
