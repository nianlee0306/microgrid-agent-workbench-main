function summary = evaluate_offline_pi(times, feedback, parameters)
%EVALUATE_OFFLINE_PI Compute a preset PI response over fixed recorded feedback.
% The caller supplies the versioned, server-side preset. Each run starts with
% fresh bias output and bumpless integral state. Outputs are arithmetic metrics.

validateattributes(times, {'double'}, {'real', 'finite', 'vector', 'nonnegative'});
validateattributes(feedback, {'double'}, {'real', 'finite', 'vector'});
validateattributes(parameters, {'double'}, {'real', 'finite', 'vector', 'numel', 10});
if numel(times) < 2 || numel(times) > 250000 || ...
        numel(times) ~= numel(feedback) || any(diff(times) <= 0)
    error('Workbench:Window', 'Expected aligned samples on a strictly increasing elapsed clock.');
end

reference = parameters(1);
errorScale = parameters(2);
bias = parameters(3);
kp = parameters(4);
ki = parameters(5);
kaw = parameters(6);
lowerBound = parameters(7);
upperBound = parameters(8);
rate = parameters(9);
controlPeriod = parameters(10);
if errorScale <= 0 || lowerBound > bias || bias > upperBound || rate <= 0 || ...
        controlPeriod <= 0 || times(end) - times(1) + 1e-12 < controlPeriod
    error('Workbench:Preset', 'The preset or requested elapsed duration is invalid.');
end

output = bias;
lastUpdate = times(1);
initialError = (reference - feedback(1)) / errorScale;
integral = -kp * initialError;
minimum = output;
maximum = output;
updates = 0;
for i = 2:numel(times)
    dt = times(i) - lastUpdate;
    if dt + 1e-12 < controlPeriod
        continue
    end
    lastUpdate = times(i);
    deviation = (reference - feedback(i)) / errorScale;
    raw = bias + kp * deviation + integral;
    saturated = min(max(raw, lowerBound), upperBound);
    change = rate * dt;
    output = min(max(saturated, output - change), output + change);
    integral = integral + dt * (ki * deviation + kaw * (output - raw));
    minimum = min(minimum, output);
    maximum = max(maximum, output);
    updates = updates + 1;
end
summary = [numel(times), updates, minimum, maximum, output];
end
