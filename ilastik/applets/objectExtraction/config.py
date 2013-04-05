# FIXME: use ilastik config file

compress_labels = False

# all these features are precalculated in opExtractObjects
# vigra_features = ['Count', 'Mean', 'Variance', 'Skewness', 'Kurtosis', 'RegionCenter']
# other_features = ['lbp', 'lbp_incl', 'lbp_excl', 'bad_slices']

vigra_features = ['Count', 'Mean']
other_features = []

# only these features are used. eventually these will be chosen
# interactively. They many include features not in 'vigra_features',
# in the case that some other features are also used.
#selected_features = ['Count', 'Mean', 'Mean_excl', 'Variance', \
#                     'Variance_excl', 'Skewness', 'Skewness_excl', \
#                      'Kurtosis', 'Kurtosis_excl', 'lbp', 'lbp_excl']
selected_features = ['Count', 'Mean']

#selected_features = ['lbp_excl', 'lbp_obj']

#selected_features = ['Histogram_excl', 'Histogram_obj', 'lbp_excl', 'lbp_obj']
