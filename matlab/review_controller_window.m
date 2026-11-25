function metrics = review_controller_window(values, signalId, reference)
%REVIEW_CONTROLLER_WINDOW Summarize a whitelisted column from a replay window.
% Output: [sample count, valid count, min, max, mean, mean deviation,
% maximum absolute deviation]. NaN denotes missing samples or an optional reference.

allowed = ["v_positive_rms", "v_negative_rms", "va_rms", "vb_rms", "vc_rms", ...
    "ia_rms", "ib_rms", "ic_rms", "i4_rms", "i_positive_rms", "i_negative_rms", ...
    "frequency_actual_hz", "frequency_feedback_hz", "frequency_raw_hz", ...
    "speed_estimated_rpm", "speed_feedback_rpm", "speed_mechanical_rpm", ...
    "controller_state", "torque_nm", "power_w", "energy_wh"];
signalId = string(signalId);
if ~isscalar(signalId) || ~ismember(signalId, allowed)
    error('Workbench:Signal', 'Signal ID is outside the offline diagnostic allowlist.');
end
validateattributes(values, {'double'}, {'real'});
validateattributes(reference, {'double'}, {'real', 'scalar'});
if (~isempty(values) && ~isvector(values)) || numel(values) > 250000 || ...
        any(isinf(values(:))) || isinf(reference)
    error('Workbench:Window', 'Expected a bounded vector and a finite or missing reference.');
end

valid = values(isfinite(values));
metrics = [numel(values), numel(valid), NaN(1, 5)];
if isempty(valid)
    return
end
metrics(3:5) = [min(valid), max(valid), mean(valid)];
if isfinite(reference)
    metrics(6:7) = [metrics(5) - reference, max(abs(valid - reference))];
end
end
