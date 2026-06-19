CTMS_MAP = {
    'Lab: Blood draw': {'activity_code': 'LAB_DRAW', 'form': 'Blood Collection', 'system': 'EDC'},
    'ECG': {'activity_code': 'ECG', 'form': 'ECG Form', 'system': 'EDC'},
    'Study Drug': {'activity_code': 'DRUG_ADMIN', 'form': 'Drug Administration', 'system': 'IRT'},
    'CT Scan': {'activity_code': 'CT_SCAN', 'form': 'Imaging Form', 'system': 'EDC'},
    'Informed Consent': {'activity_code': 'CONSENT', 'form': 'Consent Form', 'system': 'EDC'},
    'Adverse Event Review': {'activity_code': 'AE_REVIEW', 'form': 'AE Form', 'system': 'EDC'},
}


def map_to_ctms(normalized_name):
    return CTMS_MAP.get(normalized_name, {'activity_code': 'UNKNOWN', 'form': normalized_name, 'system': 'UNKNOWN'})
