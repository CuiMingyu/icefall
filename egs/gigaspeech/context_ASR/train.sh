./path.sh
./zipformer/train.py \
  --world-size 4 \
  --num-epochs 40 \
  --start-epoch 1 \
  --use-fp16 0 \
  --exp-dir zipformer/exp/context_xl_seg_emb_future_new_attention_all \
  --subset M \
  --max-duration 200 \
  --lr-epochs 3.3333
