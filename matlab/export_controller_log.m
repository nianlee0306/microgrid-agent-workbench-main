function telemetry = export_controller_log(trustedMatFile, outputCsv)
%EXPORT_CONTROLLER_LOG Export allowlisted telemetry from a trusted local MAT file.
%   T = EXPORT_CONTROLLER_LOG(INPUT_MAT, OUTPUT_CSV) also returns the exported
%   table. The original log is read only. A missing source sample index becomes
%   a zero-based index of summary rows. Elapsed times stay in their source units.

arguments
    trustedMatFile (1,1) string
    outputCsv (1,1) string
end

[~, ~, inputExtension] = fileparts(trustedMatFile);
[~, ~, outputExtension] = fileparts(outputCsv);
if ~strcmpi(inputExtension, '.mat') || ~isfile(trustedMatFile)
    error('Workbench:Input', 'Select an existing trusted local MAT file.');
end
if ~strcmpi(outputExtension, '.csv') || isfile(outputCsv) || isfolder(outputCsv)
    error('Workbench:Output', 'Select a new output CSV path.');
end

loaded = load(trustedMatFile, 'logData');
if ~isfield(loaded, 'logData') || ~isstruct(loaded.logData) || ~isscalar(loaded.logData)
    error('Workbench:Schema', 'The MAT file must contain a scalar logData struct.');
end
logData = loaded.logData;
if isfield(logData, 'summaryTable') && ~isempty(logData.summaryTable)
    summary = logData.summaryTable;
elseif isfield(logData, 'summary')
    summary = logData.summary;
else
    error('Workbench:Schema', 'The MAT file has no controller summary.');
end
if ~(istable(summary) || (isstruct(summary) && isscalar(summary)))
    error('Workbench:Schema', 'The controller summary must be a table or a scalar struct.');
end

if hasColumn(summary, 'ElapsedTime_s') && ~hasColumn(summary, 'time_s')
    clock = 'ElapsedTime_s';
    phases = {'E1_RMS', 'E2_RMS', 'E3_RMS', 'I1_RMS', 'I2_RMS', 'I3_RMS'};
elseif hasColumn(summary, 'time_s') && ~hasColumn(summary, 'ElapsedTime_s')
    clock = 'time_s';
    phases = {'Va_RMS', 'Vb_RMS', 'Vc_RMS', 'Ia_RMS', 'Ib_RMS', 'Ic_RMS'};
else
    error('Workbench:Schema', 'Expected exactly one recognized elapsed-seconds column.');
end

elapsed = summary.(clock);
validateattributes(elapsed, {'numeric'}, {'real', 'vector', 'nonempty', 'finite', 'nonnegative'});
elapsed = elapsed(:);
sampleCount = numel(elapsed);
if any(diff(elapsed) < 0)
    error('Workbench:Schema', 'Elapsed seconds decrease within the controller summary.');
end

telemetry = table(repmat("controller-log-v1", sampleCount, 1), ...
    'VariableNames', {'SchemaVersion'});
if hasColumn(summary, 'SampleIndex')
    telemetry.SampleIndex = numericColumn(summary.SampleIndex, sampleCount, 'SampleIndex');
    validateattributes(telemetry.SampleIndex, {'numeric'}, {'finite', 'nonnegative', 'integer'});
    if any(diff(telemetry.SampleIndex) <= 0)
        error('Workbench:Schema', 'Source sample indices must strictly increase.');
    end
else
    telemetry.SampleIndex = (0:sampleCount-1).';
end
telemetry.(clock) = elapsed;

if hasColumn(summary, 'Timestamp')
    timestamps = summary.Timestamp;
    if ~isdatetime(timestamps) || ~isvector(timestamps) || numel(timestamps) ~= sampleCount
        error('Workbench:Schema', 'Timestamp must be a datetime for every summary row.');
    end
    timestamps = timestamps(:);
    timestamps.Format = 'yyyy-MM-dd''T''HH:mm:ss.SSS';
    timezone = string(timestamps.TimeZone);
    if strlength(timezone) > 0
        timestamps.Format = 'yyyy-MM-dd''T''HH:mm:ss.SSSXXX';
    else
        timezone = "unknown";
    end
    timestampStrings = string(timestamps);
    timestampStrings(isnat(timestamps)) = missing;
    telemetry.Timestamp = timestampStrings;
    telemetry.TimestampTimeZone = repmat(timezone, sampleCount, 1);
end

columns = [phases, {'BlockStartElapsed_s', 'PhaseDt_s', 'I4_RMS', 'I4_mean_A', ...
    'Vpositive_RMS', 'Vnegative_RMS', 'VnegativeRatio', 'Ipositive_RMS', ...
    'Inegative_RMS', 'FrequencyRaw_Hz', 'FrequencyActual_Hz', ...
    'FrequencyFeedback_Hz', 'SpeedActual_rpm', 'SpeedFeedback_rpm', ...
    'Praw_W', 'Qraw_var', 'FrequencyError_Hz', 'IntegralVoltage_V', ...
    'AO2Unclipped_V', 'AO2Command_V', 'ControlDt_s', 'ControllerEnabled', ...
    'error_pu', 'integralDuty', 'dutyUnclipped', 'dutyCommand', 'AO1_V'}];
for i = 1:numel(columns)
    name = columns{i};
    if hasColumn(summary, name)
        telemetry.(name) = numericColumn(summary.(name), sampleCount, name);
    elseif strcmp(name, 'ControllerEnabled') && hasColumn(summary, 'controllerEnabled')
        telemetry.(name) = numericColumn(summary.controllerEnabled, sampleCount, name);
    elseif strcmp(name, 'ControlDt_s') && hasColumn(summary, 'controlDt_s')
        telemetry.(name) = numericColumn(summary.controlDt_s, sampleCount, name);
    end
end
telemetry = expandPhaseMatrix(telemetry, summary, 'Vabc_RMS', phases(1:3), sampleCount);
telemetry = expandPhaseMatrix(telemetry, summary, 'Iabc_RMS', phases(4:6), sampleCount);
if ~any(ismember(telemetry.Properties.VariableNames, ...
        [phases, {'Vpositive_RMS', 'FrequencyActual_Hz', 'FrequencyFeedback_Hz'}]))
    error('Workbench:Schema', 'No recognized controller measurements were found.');
end
writetable(telemetry, outputCsv);
end

function present = hasColumn(summary, name)
if istable(summary)
    present = ismember(name, summary.Properties.VariableNames);
else
    present = isfield(summary, name);
end
end

function values = numericColumn(values, sampleCount, name)
validateattributes(values, {'numeric', 'logical'}, {'real', 'vector', 'numel', sampleCount}, ...
    'export_controller_log', name);
values = values(:);
if any(isinf(values))
    error('Workbench:Schema', 'Column %s contains infinite values.', name);
end
end

function telemetry = expandPhaseMatrix(telemetry, summary, input, names, sampleCount)
if ~hasColumn(summary, input)
    return
end
values = summary.(input);
validateattributes(values, {'numeric'}, {'real', '2d'}, 'export_controller_log', input);
if isstruct(summary) && isequal(size(values), [3, sampleCount])
    values = values.';
end
if ~isequal(size(values), [sampleCount, 3])
    error('Workbench:Schema', 'Column %s has invalid phase-array dimensions.', input);
end
for i = 1:3
    if ~hasColumn(telemetry, names{i})
        telemetry.(names{i}) = numericColumn(values(:, i), sampleCount, names{i});
    end
end
end
