## Installation
Please refer to the [official installation guidance](https://verl.readthedocs.io/en/latest/start/install.html#install-from-custom-environment) of verl.

### Anaconda
```
# Create virtual enviroment
conda create -n verl4prm python=3.11
conda activate verl4prm

# Clone & Installation
git clone https://github.com/BiNLP/verl4prm.git
cd verl4prm
pip install vllm==0.9.2
pip install --no-deps -e .
```


## Training of PRM
```bash
cd PRM
# stage 1
bash train_stage_1.sh
# stage 2
bash train_stage_2.sh
```

### Training of LLM
Set the reward type in the [config file](verl/trainer/config/ppo_trainer.yaml):

1. `PURE-VR` uses `reward_model.enable=False reward_model.reward_manager=prime`
2. `PURE-PRM` uses `reward_model.enable=True reward_model.reward_manager=blank`
3. `PURE-PRM+VR` uses `reward_model.enable=True reward_model.reward_manager=prime`.

Then start training:

```bash
sh bash_script/train.sh
```
