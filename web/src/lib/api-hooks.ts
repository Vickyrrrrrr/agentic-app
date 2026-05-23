import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { BuildJob, Design, Profile, BillingStatus, PipelineSchema, BuildResult } from './types';

const QUERY_KEYS = {
  jobs: ['jobs'] as const,
  designs: ['designs'] as const,
  profile: ['profile'] as const,
  billing: ['billing'] as const,
  pipelineSchema: ['pipeline', 'schema'] as const,
  buildResult: (jobId: string) => ['build', 'result', jobId] as const,
  buildOptions: ['build', 'options'] as const,
};

export const useJobs = () => {
  return useQuery({
    queryKey: QUERY_KEYS.jobs,
    queryFn: async () => {
      const { data } = await api.get<{ jobs: BuildJob[] }>('/jobs');
      return data?.jobs || [];
    },
    staleTime: 1000 * 30,
  });
};

export const useDesigns = () => {
  return useQuery({
    queryKey: QUERY_KEYS.designs,
    queryFn: async () => {
      const { data } = await api.get<{ designs: Design[] }>('/designs');
      return data?.designs || [];
    },
    staleTime: 1000 * 60,
  });
};

export const useProfile = () => {
  return useQuery({
    queryKey: QUERY_KEYS.profile,
    queryFn: async () => {
      const { data } = await api.get<Profile>('/profile');
      return data;
    },
    staleTime: 1000 * 60 * 5,
  });
};

export const useBillingStatus = () => {
  return useQuery({
    queryKey: QUERY_KEYS.billing,
    queryFn: async () => {
      const { data } = await api.get<BillingStatus>('/billing/status');
      return data;
    },
    staleTime: 1000 * 60,
  });
};

export const usePipelineSchema = () => {
  return useQuery({
    queryKey: QUERY_KEYS.pipelineSchema,
    queryFn: async () => {
      const { data } = await api.get<PipelineSchema>('/pipeline/schema');
      return data;
    },
    staleTime: 1000 * 60 * 10,
  });
};

export const useBuildResult = (jobId: string | null) => {
  return useQuery({
    queryKey: QUERY_KEYS.buildResult(jobId || ''),
    queryFn: async () => {
      if (!jobId) return null;
      const { data } = await api.get<BuildResult>(`/build/result/${jobId}`);
      return data;
    },
    enabled: !!jobId,
    staleTime: 1000 * 30,
  });
};

export const useBuildOptions = () => {
  return useQuery({
    queryKey: QUERY_KEYS.buildOptions,
    queryFn: async () => {
      const { data } = await api.get('/build/options');
      return data;
    },
    staleTime: 1000 * 60 * 10,
  });
};

export const useStartBuild = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (params: {
      design_name: string;
      description: string;
      api_key?: string | null;
      skip_openlane?: boolean;
      skip_coverage?: boolean;
      full_signoff?: boolean;
      max_retries?: number;
      show_thinking?: boolean;
      min_coverage?: number;
      pdk_profile?: string;
      human_in_loop?: boolean;
    }) => {
      const { data } = await api.post<{ job_id: string }>('/build', params);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.jobs });
    },
  });
};

export const useCancelBuild = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (jobId: string) => {
      const { data } = await api.post(`/build/cancel/${jobId}`);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.jobs });
    },
  });
};

export const useApproveStage = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (params: { stage: string; design_name: string }) => {
      const { data } = await api.post('/approve', params);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.jobs });
    },
  });
};

export const useRejectStage = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (params: { stage: string; design_name: string; feedback?: string }) => {
      const { data } = await api.post('/reject', params);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.jobs });
    },
  });
};

export const useSaveByokConfig = () => {
  const queryClient = useQueryClient();
  
  return useMutation({
    mutationFn: async (config: Record<string, { api_key: string; model?: string; base_url?: string }>) => {
      const { data } = await api.post('/profile/byok', config);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.profile });
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.billing });
    },
  });
};

export const usePrefetchDesigns = () => {
  const queryClient = useQueryClient();
  
  return () => {
    queryClient.prefetchQuery({
      queryKey: QUERY_KEYS.designs,
      queryFn: async () => {
        const { data } = await api.get<{ designs: Design[] }>('/designs');
        return data?.designs || [];
      },
    });
  };
};