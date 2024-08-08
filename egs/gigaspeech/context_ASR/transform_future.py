import gzip
import json
import copy

path = "data/fbank/gigaspeech_cuts_M.jsonl.gz"
transform_path = "data/fbank/gigaspeech_cuts_M_future.jsonl"
# 打开gzip文件

data_list = []
with gzip.open(path, 'rt', encoding='utf-8') as file:
    for line in file:
        data = json.loads(line)
        data_list.append(data)

updated_data_list = []

previous_data = None
future_data = None
for i, current_data in enumerate(data_list):
    if previous_data is not None:
        previous_id = previous_data['id']
        current_id = current_data['id']
        previous_dialog = previous_id.strip().split('_')[0]
        current_dialog = current_id.strip().split('_')[0]
        if previous_dialog == current_dialog:
            current_no_change_data = copy.deepcopy(current_data)
            current_data['custom']['previous_data'] = copy.deepcopy(previous_data)
            previous_data = current_no_change_data
            
        else:
            current_no_change_data = copy.deepcopy(current_data)
            current_data['custom']['previous_data'] = copy.deepcopy(current_data)
            previous_data = current_no_change_data
    else:
        previous_data = copy.deepcopy(current_data)
        current_data['custom']['previous_data'] = previous_data

    if i < len(data_list) - 1:
        future_data = data_list[i + 1]
        future_id = future_data['id']
        future_dialog = future_id.strip().split('_')[0]
        current_id = current_data['id']
        current_dialog = current_id.strip().split('_')[0]
        if future_dialog == current_dialog:
            current_data['custom']['future_data'] = copy.deepcopy(future_data)
        else:
            current_data['custom']['future_data'] = copy.deepcopy(current_data)
    else:
        current_data['custom']['future_data'] = copy.deepcopy(current_data) 

    updated_data_list.append(current_data)
with open(transform_path, 'w', encoding='utf-8') as file:
    for data_item in updated_data_list:
        json_string = json.dumps(data_item)
        file.write(json_string + '\n')

with open(transform_path, 'rb') as f_in:
    with gzip.open(transform_path + '.gz', 'wb') as f_out:
        f_out.writelines(f_in)