MAP = {
    'blood sample': ('Lab: Blood draw', ['CBC', 'CMP']),
    'cbc': ('Lab: CBC', ['CBC']),
    'cmp': ('Lab: CMP', ['CMP']),
    'ecg': ('ECG', ['ECG']),
    'drug administration': ('Study Drug', ['Administer Drug']),
    'ct scan': ('CT Scan', ['CT Scan']),
    'informed consent': ('Informed Consent', ['Consent']),
    'ae review': ('Adverse Event Review', ['AE Review']),
}

def normalize(name: str):
    key = name.lower().strip()
    if key in MAP:
        return MAP[key]
    return name, []
