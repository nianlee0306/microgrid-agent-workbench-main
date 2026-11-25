function trace = simulate_pi_sandbox(scenario, kp, ki)
%SIMULATE_PI_SANDBOX Pure PI step response for a normalized synthetic plant.
% Rows contain time, reference, simulated output, command, and clip flag.
arguments
    scenario (1,:) char {mustBeMember(scenario, {'voltage','frequency'})}
    kp (1,1) double {mustBeFinite,mustBeGreaterThanOrEqual(kp,0),mustBeLessThanOrEqual(kp,4)}
    ki (1,1) double {mustBeFinite,mustBeGreaterThanOrEqual(ki,0),mustBeLessThanOrEqual(ki,4)}
end

if strcmp(scenario, 'voltage')
    timeConstant = 0.7;
else
    timeConstant = 1.3;
end
dt = 0.05;
stepSample = 20;
sampleCount = 501;
commandMax = 2.0;
decay = exp(-dt / timeConstant);
trace = zeros(5, sampleCount);
output = 0.0;
integral = 0.0;
for sample = 1:sampleCount
    index = sample - 1;
    reference = double(index >= stepSample);
    error = reference - output;
    raw = kp * error + integral;
    command = min(commandMax, max(0.0, raw));
    trace(:, sample) = [index * dt; reference; output; command; double(command ~= raw)];
    if ~((raw > commandMax && error > 0) || (raw < 0 && error < 0))
        integral = integral + dt * ki * error;
    end
    output = decay * output + (1.0 - decay) * command;
end
end
