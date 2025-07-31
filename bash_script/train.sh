
MODEL_PATH=/data/home/scyb224/Workspace/LLMs/Qwen2.5-1.5B-Instruct
PRM_PATH=None
TRAIN_FILES=/data/home/scyb224/Workspace/verl/data/logiqa/train.parquet
VAL_FILES=/data/home/scyb224/Workspace/verl/data/logiqa/test.parquet

python3 -m verl.trainer.main_ppo \
    data.train_files=${TRAIN_FILES} \
    data.val_files=${VAL_FILES} \
    data.prompt_key='prompt' \
    actor_rollout_ref.model.path=${MODEL_PATH} \
    actor_rollout_ref.actor.ppo_epochs=3 \
    critic.ppo_epochs=3 \
    reward_model.type='judge' \
    reward_model.enable=True \
    reward_model.model.path=${PRM_PATH} \
    reward_model.reward_manager='blank' \
    reward_model.credit_assignment='gamma-decay' \
    trainer.experiment_name='Qwen2.5-1.5B-LLMAsJudge_Epoch3' \
    trainer.total_epochs=3 \
    trainer.save_freq=10 \
    trainer.test_freq=10 \
    trainer.debug_mode=False \
    trainer.n_gpus_per_node=2 \
    trainer.nnodes=1